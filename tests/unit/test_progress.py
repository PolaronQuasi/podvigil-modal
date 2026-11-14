"""Phase clocks, safe logs and read-only worker-finalization visibility."""

from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile
import unittest

from podvigil.modal_backend import state, provider
from podvigil.modal_backend.errors import record_error
from podvigil.records import exclusive_lock
from tests.support.modal import job, spec


class Progress(unittest.TestCase):
    def test_phase_history_has_one_entry_and_log_line_per_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            with patch.object(state.time, "time", return_value=100):
                state.update(folder, record, phase="building image", deadline=400)
                state.update(folder, record)
            with patch.object(state.time, "time", return_value=150):
                state.update(folder, record, phase="waiting for container")
            snapshot = state.status(folder, now=180)
            self.assertEqual(snapshot["phase_seconds"], 30)
            self.assertEqual(snapshot["deadline_seconds_left"], 220)
            self.assertEqual([item["started_at"] for item in snapshot["phase_history"]], [100, 150])
            lines = [
                json.loads(line) for line in (folder / "watchdog.log").read_text().splitlines()
            ]
            self.assertEqual(
                [line["message"] for line in lines], ["building image", "waiting for container"]
            )

    def test_finished_status_reports_lock_owner_without_mutating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            state.update(folder, record, phase="finished", outcome="success")
            with exclusive_lock(folder / "modal-worker.lock", timeout=0):
                before = {p.name: p.read_bytes() for p in folder.iterdir() if p.suffix != ".lock"}
                snapshot = state.status(folder)
                self.assertTrue(snapshot["worker_finishing"])
                self.assertFalse(snapshot["recovery_ready"])
                self.assertEqual(
                    before,
                    {p.name: p.read_bytes() for p in folder.iterdir() if p.suffix != ".lock"},
                )
            self.assertFalse(state.status(folder)["worker_finishing"])
            self.assertTrue(state.status(folder)["recovery_ready"])
            self.assertEqual((folder / "modal-worker.lock").read_bytes(), b"0")

    def test_log_errors_do_not_include_sdk_message(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record_error(folder, ValueError("private-token\nforged phase"))
            lines = (folder / "watchdog.log").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertNotIn("private-token", lines[0])
            self.assertEqual(json.loads(lines[0])["event"], "error")

    def test_image_build_is_bounded_and_uses_the_existing_app(self):
        with (
            patch.object(provider, "sdk", return_value=Mock()),
            patch.object(provider, "bounded_create") as build,
        ):
            cloud = provider.Provider(spec())
            cloud.prepare()
            original = cloud.image.build
            cloud.build_image(30)
            build.assert_called_once_with(original, 30, app=cloud.app)
            self.assertIs(cloud.image, build.return_value)
