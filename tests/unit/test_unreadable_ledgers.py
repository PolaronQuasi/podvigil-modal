"""A damaged unrelated ledger must not prevent observing or cancelling a healthy job."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from podvigil import settings
from podvigil.cli import run_cli
from podvigil.modal_backend import state
from podvigil.records import save
from tests.support.modal import job


class UnreadableLedgers(unittest.TestCase):
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
