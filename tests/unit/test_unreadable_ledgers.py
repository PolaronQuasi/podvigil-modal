"""A damaged unrelated ledger must not prevent observing or cancelling a healthy job."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import json
import os
import tempfile
import unittest

from podvigil import settings
from podvigil.cli import run_cli
from podvigil.modal_backend import state
from podvigil.records import save
from tests.support.modal import job


class UnreadableLedgers(unittest.TestCase):
    def test_mutating_latest_refuses_newer_or_undated_skipped_jobs(self):
        for sentinel in ("newer", "mtime", "absent", "ledger_absent"):
            with self.subTest(sentinel=sentinel), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                healthy = root / "modal/jobs" / ("a" * 32)
                record = job(healthy)
                record["created_utc"] = "2026-01-01T00:00:00Z"
                record["heartbeat"] = 0
                save(healthy / "modal-job.json", record)
                broken = healthy.parent / ("b" * 32)
                broken.mkdir()
                if sentinel != "ledger_absent":
                    (broken / "modal-job.json").write_text("{")
                if sentinel != "absent":
                    save(broken / "job.json", {"created_utc": "2026-01-02T00:00:00Z"})
                if sentinel == "mtime":
                    (broken / "job.json").write_text("{")
                    os.utime(broken / "job.json", (2000000000, 2000000000))
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                for action in ("cancel", "recover"):
                    out, err = io.StringIO(), io.StringIO()
                    with (
                        redirect_stdout(out),
                        redirect_stderr(err),
                        patch(
                            "podvigil.modal_backend.provider.sdk",
                            side_effect=AssertionError("offline"),
                        ),
                    ):
                        self.assertEqual(run_cli(["--state-dir", str(root), action, "latest"]), 1)
                    message = json.loads(out.getvalue())["error"]
                    self.assertIn(str(broken), message)
                    self.assertIn("path or an explicit job ID", message)
                self.assertEqual(
                    before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                )
                for action in ("status", "wait", "logs"):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        run_cli(["--state-dir", str(root), action, "latest"])
                    result = json.loads(out.getvalue())
                    if action == "logs":
                        self.assertFalse(result["available"])
                        self.assertEqual(result["exit_code"], 0)
                    else:
                        self.assertEqual(result["ledger"], str(healthy.resolve()))

    def test_mutating_latest_accepts_proven_older_skipped_job(self):
        with tempfile.TemporaryDirectory() as directory:
            settings.configure(state_dir=directory)
            self.addCleanup(settings.configure)
            healthy = Path(directory) / "modal/jobs" / ("a" * 32)
            record = job(healthy)
            record["created_utc"] = "2026-01-02T00:00:00Z"
            save(healthy / "modal-job.json", record)
            broken = healthy.parent / ("b" * 32)
            broken.mkdir()
            (broken / "modal-job.json").write_text("{")
            save(broken / "job.json", {"created_utc": "2026-01-01T00:00:00Z"})
            # Even a newer sentinel mtime must not override its valid immutable timestamp.
            os.utime(broken / "job.json", (2000000000, 2000000000))
            self.assertEqual(
                state.find_job("latest", for_mutation=True, warn=lambda _: None), healthy.resolve()
            )

    def test_explicit_foreign_ledger_keeps_project_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            save(folder / "modal-job.json", {"provider": "unrelated"})
            with self.assertRaisesRegex(
                ValueError, "Not a Modal job ledger; use the commands for its provider"
            ):
                state.find_job(str(folder))

    def test_latest_prefix_and_cancel_skip_broken_and_non_modal_ledgers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            healthy = root / "modal/jobs" / ("a" * 32)
            record = job(healthy)
            record["created_utc"] = "2026-01-01T00:00:00Z"
            save(healthy / "modal-job.json", record)
            broken = root / "modal/jobs" / ("b" * 32)
            foreign = root / "modal/jobs" / ("c" * 32)
            for path, content in ((broken, "{"), (foreign, '{"provider":"unrelated"}')):
                path.mkdir()
                (path / "modal-job.json").write_text(content)
            before = (broken / "modal-job.json").read_bytes()
            for action, selector, expected in (
                ("status", "latest", 0),
                ("status", "aaaa", 0),
                ("cancel", "aaaa", 11),
                ("status", str(broken), 1),
            ):
                out, err = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(out),
                    redirect_stderr(err),
                    patch(
                        "podvigil.modal_backend.provider.sdk", side_effect=AssertionError("offline")
                    ),
                ):
                    self.assertEqual(
                        run_cli(["--state-dir", str(root), action, selector]), expected
                    )
                if expected == 1:
                    self.assertIn("Cannot read Modal ledger", json.loads(out.getvalue())["error"])
                    self.assertIn(str(broken), err.getvalue())
                else:
                    self.assertIn(str(broken), err.getvalue())
                    self.assertIn(str(foreign), err.getvalue())
                    self.assertEqual(json.loads(out.getvalue())["ledger"], str(healthy.resolve()))
            self.assertTrue((healthy / "cancel.json").exists())
            self.assertEqual(before, (broken / "modal-job.json").read_bytes())

    def test_permission_failure_is_skipped_and_prefix_ambiguity_still_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            settings.configure(state_dir=directory)
            self.addCleanup(settings.configure)
            paths = [Path(directory) / "modal/jobs" / ("aaaa" + str(i) * 28) for i in range(3)]
            for path in paths:
                job(path)
            original = state.load

            def read(folder):
                if Path(folder) == paths[-1]:
                    raise PermissionError("fixture")
                return original(folder)

            messages = []
            with patch.object(state, "load", side_effect=read):
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    state.find_job("aaaa", warn=messages.append)
            self.assertEqual(len(messages), 1)
            self.assertIn(str(paths[-1]), messages[0])
