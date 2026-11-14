"""Modal lifecycle refusals and verified recovery, without importing the SDK."""

from pathlib import Path
import json
import tempfile
import time
import unittest

from podvigil.modal_backend.recovery import recover_outputs
from podvigil.modal_backend.runner import execute
from podvigil.modal_backend.state import load, status, wait
from podvigil.records import save
from tests.support.modal import FakeModal, job


class ModalLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = job(self.folder)
        self.provider = FakeModal()
        self.now = time.time()

    def run_job(self):
        execute(
            self.folder,
            self.provider,
            clock=lambda: self.now,
            sleep=lambda seconds: setattr(self, "now", self.now + seconds),
        )
        return load(self.folder)

    def test_success_stops_before_download_and_deletes_after_two_reads(self):
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["sandbox"]["state"], "stopped")
        self.assertEqual(result["volume"]["state"], "deleted")
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)
        self.assertEqual(self.provider.calls.count("create_volume"), 1)
        self.assertGreaterEqual(self.provider.calls.count("read:outputs/result.txt"), 2)
        first_read = next(i for i, s in enumerate(self.provider.calls) if s.startswith("read:"))
        self.assertGreaterEqual(self.provider.calls[:first_read].count("poll"), 3)
        self.assertEqual(
            (Path(result["backup"]["path"]) / "outputs/result.txt").read_bytes(),
            self.provider.data["outputs/result.txt"],
        )

    def test_workload_failure_still_backs_up_and_cleans(self):
        self.provider.training_outcome = "training_failed"
        self.assertEqual(self.run_job()["outcome"], "workload_failed")
        self.assertTrue(self.provider.deleted)

    def test_ambiguous_volume_is_never_repeated(self):
        self.provider.failure = "create_volume"
        result = self.run_job()
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(self.provider.calls, ["prepare", "build_image", "create_volume"])

    def test_ambiguous_sandbox_keeps_volume_without_retry(self):
        self.provider.failure = "create_sandbox"
        result = self.run_job()
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["volume"]["state"], "exists")
        self.assertNotIn("delete_volume", self.provider.calls)
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)

    def test_unknown_provider_errors_do_not_leak(self):
        self.provider.failure = "upload"
        result = self.run_job()
        self.assertEqual(result["outcome"], "error")
        self.assertNotIn("simulated secret", json.dumps(result))

    def test_preflight_failure_creates_nothing(self):
        self.provider.failure = "prepare"
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertEqual(self.provider.calls, ["prepare"])

    def test_cancel_before_create_is_resource_free(self):
        save(self.folder / "cancel.json", {})
        self.assertEqual(self.run_job()["outcome"], "cancelled")
        self.assertEqual(self.provider.calls, [])

    def test_expired_upload_never_starts_compute(self):
        self.provider.hook = lambda name: (
            setattr(self, "now", self.now + 200) if name == "upload" else None
        )
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertNotIn("create_sandbox", self.provider.calls)
        self.assertTrue(self.provider.deleted)

    def test_deadline_terminates_and_retains_missing_manifest(self):
        self.provider.finish = False
        self.assertEqual(self.run_job()["outcome"], "storage_retained")
        self.assertIn("terminate", self.provider.calls)
        self.assertFalse(self.provider.deleted)

    def test_cancel_during_run_stops_compute(self):
        self.provider.finish = False
        self.provider.hook = lambda name: (
            save(self.folder / "cancel.json", {}) if name == "poll" else None
        )
        result = self.run_job()
        self.assertTrue(result["cancel_requested"])
        self.assertIn("terminate", self.provider.calls)
        self.assertFalse(self.provider.deleted)

    def test_accepted_termination_without_proof_never_reads_outputs(self):
        self.provider.finish = self.provider.termination_works = False
        self.assertEqual(self.run_job()["outcome"], "cleanup_unverified")
        self.assertFalse(any(x.startswith("read:") for x in self.provider.calls))

    def test_missing_manifest_requires_explicit_partial_recovery(self):
        self.provider.publish_manifest = False
        result = self.run_job()
        self.assertEqual(result["outcome"], "storage_retained")
        outcome = recover_outputs(
            self.provider, self.folder, result, accept_partial=True, sleep=lambda _: None
        )
        self.assertEqual(outcome, "partial_outputs")
        self.assertTrue(self.provider.deleted)
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)

    def mutate_at_inventory(self, change):
        done = []

        def hook(name):
            if name == "files" and not done:
                done.append(True)
                change()

        self.provider.hook = hook

    def test_wrong_owner_keeps_storage(self):
        self.mutate_at_inventory(lambda: self.provider.data.__setitem__("owner.json", b"{}"))
        self.assertEqual(self.run_job()["outcome"], "storage_retained")
        self.assertFalse(self.provider.deleted)

    def test_corrupted_output_keeps_storage(self):
        self.mutate_at_inventory(
            lambda: self.provider.data.__setitem__("outputs/result.txt", b"corrupt")
        )
        self.assertEqual(self.run_job()["outcome"], "storage_retained")
        self.assertFalse(self.provider.deleted)

    def test_unmanifested_file_keeps_storage(self):
        self.mutate_at_inventory(lambda: self.provider.data.__setitem__("extra.bin", b"hidden"))
        self.assertEqual(self.run_job()["outcome"], "storage_retained")

    def test_traversal_refused_before_writing_any_backup(self):
        self.mutate_at_inventory(lambda: self.provider.data.__setitem__("../escape", b"bad"))
        self.assertEqual(self.run_job()["outcome"], "storage_retained")
        self.assertEqual(list(self.folder.glob("backup-*")), [])

    def test_changed_second_read_keeps_storage(self):
        reads = []

        def hook(name):
            if name == "read:outputs/result.txt":
                reads.append(True)
                if len(reads) == 2:
                    self.provider.data["outputs/result.txt"] = b"changed"

        self.provider.hook = hook
        self.assertEqual(self.run_job()["outcome"], "storage_retained")

    def test_manifest_changed_after_parsing_keeps_storage(self):
        reads = []

        def hook(name):
            if name == "read:manifest.json":
                reads.append(True)
                if len(reads) == 2:
                    original = self.provider.data["manifest.json"]
                    changed = json.loads(original)
                    entry = changed["files"]["outputs/result.txt"]
                    digest = entry["sha256"]
                    entry["sha256"] = ("0" if digest[0] != "0" else "1") + digest[1:]
                    self.provider.data["manifest.json"] = json.dumps(changed).encode()
                    self.assertEqual(len(self.provider.data["manifest.json"]), len(original))

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(result["outcome"], "storage_retained")
        self.assertFalse(self.provider.deleted)
        self.assertNotIn("delete_volume", self.provider.calls)
        self.assertNotIn("backup", result)

    def test_backup_limit_refuses_download(self):
        self.job["config"]["max_backup_bytes"] = 1
        save(self.folder / "modal-job.json", self.job)
        self.assertEqual(self.run_job()["outcome"], "error")
        self.assertNotIn("create_volume", self.provider.calls)
        self.assertFalse(any(x.startswith("read:") for x in self.provider.calls))

    def test_delete_ack_without_absence_is_storage_retained(self):
        self.provider.deletion_works = False
        self.assertEqual(self.run_job()["outcome"], "storage_retained")

    def test_lost_delete_response_can_be_recovered_without_recreating(self):
        self.provider.failure = "delete_volume"
        result = self.run_job()
        self.assertEqual(result["outcome"], "storage_retained")
        self.provider.deleted = True
        self.provider.failure = None
        outcome = recover_outputs(self.provider, self.folder, result, sleep=lambda _: None)
        self.assertEqual(outcome, "success")
        self.assertEqual(result["volume"]["state"], "deleted")

    def test_wait_timeout_is_read_only(self):
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        result, outcome = wait(self.folder, 0)
        self.assertFalse(result["ended"])
        self.assertEqual(outcome, "wait_timeout")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.folder.iterdir()})

    def test_wait_dead_watchdog_stops_and_reports_existing_compute(self):
        self.job["heartbeat"] = 0
        self.job["sandbox"]["state"] = "exists"
        save(self.folder / "modal-job.json", self.job)
        lines = []
        result, outcome = wait(self.folder, progress=lines.append)
        self.assertEqual(outcome, "cleanup_unverified")
        self.assertFalse(result["watchdog_heard"])
        self.assertIn("no longer heard", lines[-1])

    def test_wait_completed_job_returns_same_outcome(self):
        self.run_job()
        result, outcome = wait(self.folder)
        self.assertTrue(result["ended"])
        self.assertEqual(outcome, "success")

    def test_wait_logs_phase_changes_only(self):
        ticks, lines = [0], []

        def sleep(_):
            ticks[0] += 1
            if ticks[0] == 3:
                self.job.update(phase="finished", outcome="workload_failed")
                save(self.folder / "modal-job.json", self.job)

        _, outcome = wait(self.folder, sleep=sleep, progress=lines.append)
        self.assertEqual(outcome, "workload_failed")
        self.assertEqual(lines, ["Modal job: armed", "Modal job: finished"])

    def test_heartbeat_is_shared_by_readers(self):
        self.job["heartbeat"] = 0
        save(self.folder / "modal-job.json", self.job)
        save(self.folder / "modal-heartbeat.json", {"at": time.time()})
        self.assertTrue(status(self.folder)["watchdog_heard"])

    def test_restarting_an_attempt_never_creates_again(self):
        self.run_job()
        before = list(self.provider.calls)
        with self.assertRaisesRegex(RuntimeError, "cannot be launched again"):
            self.run_job()
        self.assertEqual(self.provider.calls, before)

    def test_lost_create_receipt_can_resolve_only_with_provider_ownership_proof(self):
        self.provider.failure = "create_sandbox"
        self.provider.resolve_sandbox = lambda record: "sb-test"
        result = self.run_job()
        self.assertEqual(result["sandbox"]["state"], "stopped")
        self.assertEqual(result["sandbox"]["evidence"], "creation intent and both ownership tags")
        self.assertEqual(result["outcome"], "storage_retained")
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)
