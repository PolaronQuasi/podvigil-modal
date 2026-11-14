"""Log observers read cached tails; only the worker uses the owned provider session."""

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import io
import asyncio
import json
import tempfile
import time
import unittest

from podvigil.cli import run_cli
from podvigil.modal_backend import logs, provider, runner
from podvigil.modal_backend.errors import UserError
from tests.support.modal import FakeModal, job, spec


class LogTails(unittest.TestCase):
    def test_observer_reads_only_local_snapshot_and_respects_line_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job(folder)
            logs.publish(
                folder, {"stdout": "first\nsecond\nthird\n", "stderr": "warning"}, source="test"
            )
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            out = io.StringIO()
            with (
                patch.object(provider, "sdk", side_effect=AssertionError("offline")),
                redirect_stdout(out),
            ):
                self.assertEqual(run_cli(["logs", str(folder), "--lines", "2"]), 0)
            self.assertEqual(json.loads(out.getvalue())["stdout"], "second\nthird")
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_absent_mirror_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertFalse(logs.snapshot(folder)["available"])
            self.assertEqual(list(folder.iterdir()), [])

    def test_worker_mirrors_bounded_tail_and_failure_does_not_stop_compute(self):
        for failure in (None, "log_tail"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                record = job(folder)
                now = [time.time()]
                record["deadline"] = now[0] + 12
                record["sandbox"].update(state="exists", id="sb-test")
                cloud = FakeModal()
                cloud.finish = False
                cloud.failure = failure
                runner.monitor(
                    cloud,
                    folder,
                    record,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                )
                self.assertIn("log_tail", cloud.calls)
                self.assertNotIn("terminate", cloud.calls)
                self.assertEqual(logs.snapshot(folder)["available"], failure is None)
                self.assertNotIn("simulated secret", (folder / "modal-job.json").read_text())

    def test_final_logs_come_from_verified_backup_even_with_encoded_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            backup = folder / "backup"
            backup.mkdir()
            (backup / "encoded").write_bytes(b"x" * 40000 + b"\nlast line")
            logs.from_backup(folder, backup, {"outputs/stdout.log": "encoded"})
            result = logs.snapshot(folder)
            self.assertEqual(result["source"], "verified backup")
            self.assertLessEqual(len(result["stdout"]), logs.TAIL_BYTES)
            self.assertTrue(result["stdout"].endswith("last line"))

    def test_provider_exec_is_bounded_and_requires_both_ownership_tags(self):
        sdk = Mock()
        with patch.object(provider, "sdk", return_value=sdk):
            cloud = provider.Provider(spec())
        record = {"job_id": "job", "owner": "owner", "sandbox": {"id": "sb-test"}}
        sandbox = sdk.Sandbox.from_id.return_value
        sdk.Sandbox.from_id.aio = AsyncMock(return_value=sandbox)
        sandbox.get_tags.return_value = {"podvigil_job": "job", "podvigil_owner": "owner"}
        sandbox.get_tags.aio = AsyncMock(side_effect=lambda: sandbox.get_tags.return_value)
        process = Mock()
        process.stdout.read.aio = AsyncMock(return_value='{"stdout":"hello","stderr":""}')
        process.wait.aio = AsyncMock(return_value=0)
        sandbox.exec.aio = AsyncMock(return_value=process)
        self.assertEqual(cloud.log_tail(record)["stdout"], "hello")
        self.assertEqual(sandbox.exec.aio.call_args.kwargs["timeout"], 5)
        sandbox.get_tags.return_value = {}
        with self.assertRaisesRegex(UserError, "ownership"):
            cloud.log_tail(record)
        self.assertEqual(sandbox.exec.aio.call_count, 1)

        sandbox.get_tags.return_value = {"podvigil_job": "job", "podvigil_owner": "owner"}
        process.wait.aio.return_value = 1
        with self.assertRaisesRegex(UserError, "snapshot"):
            cloud.log_tail(record)

    def test_log_allowance_cancels_lookup_tags_and_stream_reads(self):
        for blocked in ("lookup", "tags", "exec", "read", "wait"):
            with self.subTest(blocked=blocked):
                sdk = Mock()
                with patch.object(provider, "sdk", return_value=sdk):
                    cloud = provider.Provider(spec())
                record = {"job_id": "job", "owner": "owner", "sandbox": {"id": "sb-test"}}
                sandbox, process = Mock(), Mock()
                cancelled = []

                async def stalled(*args, **kwargs):
                    try:
                        await asyncio.sleep(60)
                    finally:
                        cancelled.append(blocked)

                calls = {
                    "lookup": AsyncMock(return_value=sandbox),
                    "tags": AsyncMock(
                        return_value={"podvigil_job": "job", "podvigil_owner": "owner"}
                    ),
                    "exec": AsyncMock(return_value=process),
                    "read": AsyncMock(return_value='{"stdout":"ok","stderr":""}'),
                    "wait": AsyncMock(return_value=0),
                }
                calls[blocked].side_effect = stalled
                sdk.Sandbox.from_id.aio = calls["lookup"]
                sandbox.get_tags.aio = calls["tags"]
                sandbox.exec.aio = calls["exec"]
                process.stdout.read.aio = calls["read"]
                process.wait.aio = calls["wait"]
                sdk.Sandbox.from_id.side_effect = AssertionError("Unbounded synchronous lookup")
                sandbox.get_tags.side_effect = AssertionError("Unbounded synchronous tags")
                with self.assertRaises(asyncio.TimeoutError):
                    cloud.log_tail(record, allowance=0.01)
                self.assertEqual(cancelled, [blocked])
                if blocked in ("lookup", "tags"):
                    calls["exec"].assert_not_called()
