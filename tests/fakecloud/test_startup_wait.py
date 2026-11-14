"""A slow provider create is still pending while its fixed start window remains open."""

from pathlib import Path
from unittest.mock import patch
import tempfile
import time
import unittest

from podvigil.modal_backend import runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class StartupWaiting(unittest.TestCase):
    def test_create_blocked_beyond_stale_threshold_does_not_make_wait_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            record["config"]["startup_timeout_seconds"] = 600
            save(folder / "modal-job.json", record)
            cloud = FakeModal()
            now = [time.time()]
            original_status = state.status
            checked = []

            def hook(name):
                if name == "create_sandbox":
                    # Simulated blocking create: advance 180 seconds with its heartbeat still alive.
                    now[0] += 180
                    save(folder / "modal-heartbeat.json", {"at": now[0]})
                    snapshot = original_status(folder, now=now[0])
                    self.assertTrue(snapshot["loop_heard"])
                    self.assertFalse(snapshot["loop_progress_recent"])
                    self.assertTrue(snapshot["watchdog_heard"])
                    self.assertTrue(snapshot["startup_waiting"])
                    before = (folder / "modal-job.json").read_bytes()
                    with patch.object(
                        state,
                        "status",
                        side_effect=lambda folder: original_status(folder, now=now[0]),
                    ):
                        _, outcome = state.wait(folder, timeout=0)
                    self.assertEqual(outcome, "wait_timeout")
                    self.assertEqual(before, (folder / "modal-job.json").read_bytes())
                    checked.append(True)

            cloud.hook = hook
            runner.execute(
                folder,
                cloud,
                clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
            self.assertEqual(checked, [True])
            self.assertEqual(state.load(folder)["outcome"], "success")
            self.assertEqual(cloud.calls.count("create_sandbox"), 1)

    def test_startup_exception_has_a_fixed_end_and_needs_a_live_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            record = job(folder)
            record.update(
                phase="creating compute",
                heartbeat=0,
                progress_at=0,
                deadline=1000,
                startup_deadline=500,
            )
            record["sandbox"]["state"] = "unknown"
            save(folder / "modal-job.json", record)
            self.assertTrue(state.status(folder, now=400)["startup_waiting"])
            self.assertFalse(state.status(folder, now=500)["startup_waiting"])
            with patch.object(state, "status", return_value=state.status(folder, now=400)):
                self.assertEqual(
                    state.wait(folder)[1], "ambiguous"
                )  # Dead worker, not mere scheduling.
