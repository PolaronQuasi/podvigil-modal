"""A preflight refusal is a command result, not a terminal job event."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import time
import unittest

from podvigil.cli import run_cli
from podvigil.modal_backend import runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class RecoveryRefusal(unittest.TestCase):
    def test_cli_refusal_preserves_ledger_and_wait_does_not_end(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            record.update(phase="running", deadline=time.time() + 300)
            record["sandbox"].update(state="exists", id="sb-test")
            record["volume"].update(state="exists", id="vo-test")
            save(folder / "modal-job.json", record)
            before = (folder / "modal-job.json").read_bytes()
            cloud = FakeModal()
            cloud.finish = False
            out = io.StringIO()
            with (
                patch.object(runner, "_check_identity"),
                patch.object(runner, "Provider", return_value=cloud),
                redirect_stdout(out),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(run_cli(["recover", str(folder)]), 7)
            result = json.loads(out.getvalue())
            self.assertEqual(result["outcome"], "cleanup_unverified")
            self.assertIsNone(result["job_outcome"])
            self.assertFalse(result["ended"])
            self.assertEqual(before, (folder / "modal-job.json").read_bytes())
            self.assertEqual(state.wait(folder, timeout=0)[1], "wait_timeout")
            self.assertEqual(cloud.calls, ["poll"])
            cloud.failure = "poll"
            result, outcome = runner.recover(folder, provider=cloud)
            self.assertEqual(outcome, "cleanup_unverified")
            self.assertFalse(result["ended"])
            self.assertEqual(before, (folder / "modal-job.json").read_bytes())
