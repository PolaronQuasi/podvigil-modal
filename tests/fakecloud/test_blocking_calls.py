"""A recorded call allowance suppresses false stalls, without heartbeat progress writes."""

from pathlib import Path
from unittest.mock import patch
import tempfile
import threading
import time
import unittest

from podvigil.modal_backend import runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class BlockingCalls(unittest.TestCase):
    def test_real_blocked_fake_call_has_a_finite_observer_allowance(self):
        for call_name in ("build_image", "create_volume", "upload", "create_sandbox"):
            with self.subTest(call=call_name), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                record = job(folder)
                record["config"]["timeout_seconds"] = 600
                record["config"]["max_compute_cost_usd"] = 1
                save(folder / "modal-job.json", record)
                entered, release = threading.Event(), threading.Event()
                cloud = FakeModal()

                def hook(name):
                    if name == call_name:
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("Test failed to release fake call")

                cloud.hook = hook
                thread = threading.Thread(
                    target=lambda: runner.execute(folder, cloud, sleep=lambda _: None)
                )
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    call = state.load(folder)["blocking_call"]
                    now = call["started_at"] + 130
                    save(folder / "modal-heartbeat.json", {"at": now})
                    snapshot = state.status(folder, now=now)
                    self.assertEqual(snapshot["blocking_call"]["name"], call_name)
                    self.assertTrue(snapshot["loop_heard"])
                    self.assertFalse(snapshot["loop_progress_recent"])
                    with patch.object(state, "status", return_value=snapshot):
                        self.assertEqual(state.wait(folder, timeout=0)[1], "wait_timeout")
                    expired = state.status(folder, now=call["until"] + 1)
                    self.assertFalse(expired["loop_heard"])
                    self.assertFalse(expired["blocking_call_active"])
                finally:
                    release.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(state.load(folder)["blocking_call"])
                self.assertEqual(state.load(folder)["outcome"], "success")
                self.assertEqual(
                    [
                        c
                        for c in cloud.calls
                        if c
                        in ("prepare", "build_image", "create_volume", "upload", "create_sandbox")
                    ],
                    ["prepare", "build_image", "create_volume", "upload", "create_sandbox"],
                )
