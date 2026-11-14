"""The job inventory is an offline, read-only projection, including broken ledgers."""

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest
from podvigil.cli import run_cli
from podvigil.records import save
from tests.support.modal import job


class JobList(unittest.TestCase):
    def test_lists_each_job_without_credentials_or_record_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(3):
                folder = root / "modal/jobs" / str(number)
                record = job(folder)
                record.update(
                    job_id=str(number),
                    created_utc="2026-01-01T00:00:00Z",
                    runtime_timing={"supervisor_seconds": 36},
                )
                save(folder / "modal-job.json", record)
            broken = root / "modal/jobs/broken"
            broken.mkdir()
            (broken / "modal-job.json").write_text("{")
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            out = io.StringIO()
            with (
                patch("podvigil.modal_backend.provider.sdk", side_effect=AssertionError("offline")),
                redirect_stdout(out),
            ):
                self.assertEqual(run_cli(["--state-dir", str(root), "list"]), 0)
            rows = json.loads(out.getvalue())["jobs"]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["estimated_spend_usd"], 0.01)
            self.assertIn("error", rows[-1])
            self.assertEqual(
                before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            )

    def test_missing_state_root_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            missing = Path(directory) / "absent"
            self.assertEqual(run_cli(["--state-dir", str(missing), "list"]), 0)
            self.assertFalse(missing.exists())
