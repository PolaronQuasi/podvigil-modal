"""Failure injection at termination, backup durability and recovery commit boundaries."""

from pathlib import Path
from unittest.mock import Mock, patch
import os
import stat
import tempfile
import time
import unittest

from podvigil.modal_backend import recovery, runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class RecoveryFaults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = job(self.folder)
        self.provider = FakeModal()
        self.now = time.time()

    def run_job(self):
        def advance(seconds):
            self.now += seconds

        runner.execute(self.folder, self.provider, clock=lambda: self.now, sleep=advance)
        return state.load(self.folder)

    def test_full_disk_after_compute_create_still_attempts_termination(self):
        self.provider.finish = False
        disk_failed = []
        self.provider.hook = lambda name: (
            disk_failed.append(True) if name == "create_sandbox" else None
        )
        original_save = state.save

        def failing_save(path, record):
            if disk_failed:
                raise OSError("simulated disk full")
            original_save(path, record)

        with patch.object(state, "save", side_effect=failing_save), self.assertRaises(OSError):
            self.run_job()
        self.assertIn("terminate", self.provider.calls)
        self.assertFalse(self.provider.running)
        self.assertNotIn("delete_volume", self.provider.calls)

    def test_unavailable_poll_does_not_block_termination_or_claim_completion(self):
        self.provider.finish = False
        self.provider.failure = "poll"
        result = self.run_job()
        self.assertIn("terminate", self.provider.calls)
        self.assertFalse(self.provider.running)
        self.assertEqual(result["outcome"], "cleanup_unverified")
        self.assertFalse(any(name.startswith("read:") for name in self.provider.calls))
        self.assertNotIn("delete_volume", self.provider.calls)

    def test_recovery_attempts_termination_even_if_its_first_write_fails(self):
        self.job["sandbox"].update(state="exists", id="sb-test")
        self.job["volume"].update(state="exists", id="vo-test")
        save(self.folder / "modal-job.json", self.job)
        self.provider.finish = False
        with (
            patch.object(state, "save", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            runner.recover(self.folder, provider=self.provider, stop_running=True)
        self.assertFalse(self.provider.running)
        self.assertIn("terminate", self.provider.calls)
        self.assertNotIn("delete_volume", self.provider.calls)

    def test_lost_termination_response_still_requires_two_terminal_reads(self):
        self.job["sandbox"].update(state="exists", id="sb-test")
        self.provider.finish = False
        original_terminate = self.provider.terminate

        def terminate(record):
            original_terminate(record)
            raise OSError("lost response")

        self.provider.terminate = terminate
        recovery.stop_compute(self.provider, self.folder, self.job, sleep=lambda _: None)
        self.assertEqual(self.job["sandbox"]["state"], "stopped")
        self.assertEqual(self.provider.calls, ["poll", "terminate", "poll", "poll"])

    def test_directory_flush_failure_retains_remote_copy(self):
        with patch.object(
            recovery, "flush_backup_directories", side_effect=OSError("flush failed")
        ):
            result = self.run_job()
        self.assertEqual(result["outcome"], "storage_retained")
        self.assertNotIn("delete_volume", self.provider.calls)
        self.assertNotIn("backup", result)

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync")
    def test_every_backup_directory_is_flushed_before_remote_deletion(self):
        self.provider.data["outputs/nested/checkpoint.bin"] = b"checkpoint"
        synced = set()
        real_fsync = os.fsync
        checked = []

        def identity(info):
            return info.st_dev, info.st_ino

        def fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                synced.add(identity(info))
            real_fsync(fd)

        def before_delete(name):
            if name == "delete_volume":
                backup = Path(state.load(self.folder)["backup"]["path"])
                directories = [backup.parent, backup, *[p for p in backup.rglob("*") if p.is_dir()]]
                self.assertTrue(all(identity(p.stat()) in synced for p in directories))
                checked.append(True)

        self.provider.hook = before_delete
        with patch.object(os, "fsync", side_effect=fsync):
            result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(checked, [True])

    def test_recovery_restores_audited_outcome_after_deletion_without_cloud(self):
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        for outcome in ("success", "workload_failed", "partial_outputs", "cancelled"):
            for closing in (None, "error"):
                with self.subTest(outcome=outcome, closing=closing):
                    result.update(phase="deleting storage", outcome=closing)
                    result["backup"]["outcome"] = outcome
                    save(self.folder / "modal-job.json", result)
                    forbidden = Mock(side_effect=AssertionError("No cloud needed"))
                    with (
                        patch.object(runner, "Provider", forbidden),
                        patch.object(runner, "_check_identity", forbidden),
                    ):
                        document, recovered = runner.recover(self.folder)
                    self.assertEqual(recovered, outcome)
                    self.assertTrue(document["ended"])
                    self.assertEqual(state.wait(self.folder)[1], outcome)
                    forbidden.assert_not_called()

    def test_backup_receipt_alone_does_not_prove_cleanup(self):
        self.job.update(delete_requested=True, backup={"outcome": "success"})
        for sandbox, volume in (
            ("exists", "deleted"),
            ("stopped", "exists"),
            ("unknown", "deleted"),
        ):
            self.job["sandbox"]["state"] = sandbox
            self.job["volume"]["state"] = volume
            self.assertIsNone(recovery.completed_outcome(self.job))
