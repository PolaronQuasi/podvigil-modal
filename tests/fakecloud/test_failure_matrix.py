"""Fault injection across each ledger write and provider call of a complete job."""

from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import tempfile
import time
import unittest

from podvigil.modal_backend import runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class FailureMatrix(unittest.TestCase):
    def exercise(self, *, write_fault=None, after_write=False, call_fault=None):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job(folder)
            provider = FakeModal()
            now = [time.time()]
            writes, calls, fired = [0], [0], []
            original_save = state.save

            def persist(path, record):
                writes[0] += 1
                fail = writes[0] == write_fault
                if fail and not after_write:
                    fired.append(True)
                    raise OSError("injected ledger write failure")
                original_save(path, record)
                if fail:
                    fired.append(True)
                    raise OSError("injected lost persistence acknowledgement")

            def call(name):
                calls[0] += 1
                if calls[0] == call_fault:
                    fired.append(True)
                    raise ConnectionError("injected provider failure")

            provider.hook = call
            with patch.object(state, "save", side_effect=persist):
                try:
                    runner.execute(
                        folder,
                        provider,
                        clock=lambda: now[0],
                        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    )
                except OSError:
                    # The first write is outside execute's owned lifecycle. No cloud may run.
                    self.assertEqual(provider.calls, [])
            record = state.load(folder)
            self.assertLessEqual(provider.calls.count("create_volume"), 1)
            self.assertLessEqual(provider.calls.count("create_sandbox"), 1)
            if record["sandbox"]["state"] == "unknown":
                self.assertFalse(provider.deleted)
            if provider.deleted:
                self.assertIn(record["sandbox"]["state"], ("not_created", "stopped"))
                backup = Path(record["backup"]["path"])
                verification = json.loads(backup.with_suffix(".verification.json").read_text())
                self.assertEqual(set(verification["files"]), set(provider.data))
                for name, value in provider.data.items():
                    target = backup / verification["remote_to_local"][name]
                    self.assertEqual(target.read_bytes(), value)
                    self.assertEqual(
                        verification["files"][name],
                        {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()},
                    )
            if write_fault or call_fault:
                self.assertEqual(fired, [True], "The requested boundary must actually be exercised")
            return writes[0], calls[0]

    def test_each_ledger_write_failure_preserves_cleanup_evidence(self):
        writes, _ = self.exercise()
        for after in (False, True):
            for boundary in range(1, writes + 1):
                with self.subTest(write=boundary, after=after):
                    self.exercise(write_fault=boundary, after_write=after)

    def test_each_provider_failure_preserves_backup_and_never_recreates(self):
        _, calls = self.exercise()
        for boundary in range(1, calls + 1):
            with self.subTest(call=boundary):
                self.exercise(call_fault=boundary)

    def test_recovery_honors_saved_cancellation_with_graceful_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            record["sandbox"].update(state="exists", id="sb-test")
            record["volume"].update(state="exists", id="vo-test")
            record["deadline"] = time.time() + 60
            save(folder / "modal-job.json", record)
            save(folder / "cancel.json", {"requested": True})
            provider = FakeModal()
            provider.finish = False
            provider.data["owner.json"] = json.dumps(
                {"job_id": record["job_id"], "owner": record["owner"]}
            ).encode()

            def request_stop(current):
                provider.call("request_stop")
                provider.training_outcome = "cancelled"
                provider.publish(current)
                provider.running = False

            provider.request_stop = request_stop
            result, outcome = runner.recover(folder, provider=provider)
            self.assertIn("request_stop", provider.calls)
            self.assertNotIn("terminate", provider.calls)
            self.assertEqual(outcome, "cancelled")
            self.assertEqual(result["volume"]["state"], "deleted")
