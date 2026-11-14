"""Unknown creates close only by explicit, delayed assertion plus verified storage backup."""

from pathlib import Path
from unittest.mock import Mock, patch
import tempfile
import time
import unittest

from podvigil.modal_backend import runner, recovery, state
from podvigil.modal_backend.errors import LifecycleError
from podvigil.records import save
from tests.support.modal import FakeModal, job


class OperatorClose(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        job(self.folder)
        self.cloud = FakeModal()
        self.cloud.failure = "create_sandbox"
        runner.execute(self.folder, self.cloud, sleep=lambda _: None)
        self.cloud.failure = None
        self.record = state.load(self.folder)
        self.record["deadline"] = time.time() - self.record["config"]["timeout_seconds"] - 10
        save(self.folder / "modal-job.json", self.record)

    def close(self, **kwargs):
        with patch.object(recovery.time, "sleep"):
            return runner.recover(self.folder, provider=self.cloud, **kwargs)

    def test_time_and_lookup_miss_never_close_without_the_flag(self):
        _, outcome = self.close()
        self.assertEqual(outcome, "ambiguous")
        self.assertFalse(self.cloud.deleted)

    def test_explicit_close_verifies_backup_and_records_statement_not_terminal_state(self):
        result, outcome = self.close(assert_no_sandbox=True)
        self.assertEqual(outcome, "error")
        self.assertTrue(self.cloud.deleted)
        self.assertEqual(result["sandbox"]["state"], "operator_asserted_absent")
        self.assertIn("not an observation", result["sandbox"]["operator_assertion"]["statement"])
        self.assertTrue(Path(result["backup"]["path"]).is_dir())
        self.assertNotIn("terminal_observations", state.load(self.folder))
        self.assertGreaterEqual(self.cloud.calls.count("read:run.json"), 2)
        self.assertEqual(self.cloud.calls.count("create_sandbox"), 1)
        self.assertEqual(recovery.completed_outcome(state.load(self.folder)), "error")
        invalid = state.load(self.folder)
        invalid["backup"]["outcome"] = "success"
        self.assertIsNone(recovery.completed_outcome(invalid))

    def test_before_cutoff_or_missing_deadline_refuses_without_changing_job(self):
        for deadline in (time.time(), None):
            with self.subTest(deadline=deadline):
                self.record["deadline"] = deadline
                save(self.folder / "modal-job.json", self.record)
                before = (self.folder / "modal-job.json").read_bytes()
                with self.assertRaisesRegex(LifecycleError, "after|recorded fixed deadline"):
                    self.close(assert_no_sandbox=True)
                self.assertEqual((self.folder / "modal-job.json").read_bytes(), before)
                self.assertFalse(self.cloud.deleted)

    def test_found_sandbox_saves_receipt_and_refuses_assertion(self):
        self.cloud.resolve_sandbox = Mock(return_value="sb-owned")
        with self.assertRaisesRegex(LifecycleError, "found"):
            self.close(assert_no_sandbox=True)
        self.assertEqual(state.load(self.folder)["sandbox"]["id"], "sb-owned")
        self.cloud.resolve_sandbox.assert_called_once()
        self.assertFalse(self.cloud.deleted)
        self.assertNotIn("terminate", self.cloud.calls)

    def test_failed_lookup_or_conflicting_flags_cannot_record_assertion(self):
        self.cloud.resolve_sandbox = Mock(side_effect=OSError("fake lookup failure"))
        with self.assertRaises(OSError):
            self.close(assert_no_sandbox=True)
        self.assertEqual(state.load(self.folder)["sandbox"]["state"], "unknown")
        for options in ({"stop_running": True}, {"sandbox_id": "sb-owned"}):
            with self.assertRaises(LifecycleError):
                self.close(assert_no_sandbox=True, **options)
        self.assertFalse(self.cloud.deleted)

    def test_changed_upload_or_extra_file_is_retained_despite_assertion(self):
        self.cloud.data["run.json"] = b"changed"
        result, outcome = self.close(assert_no_sandbox=True)
        self.assertEqual(outcome, "storage_retained")
        self.assertFalse(self.cloud.deleted)
        self.assertEqual(result["sandbox"]["state"], "operator_asserted_absent")

    def test_recovery_checks_again_if_a_resource_appears_after_assertion(self):
        self.cloud.failure = "files"
        self.close(assert_no_sandbox=True)
        self.cloud.failure = None
        self.cloud.resolve_sandbox = Mock(return_value="sb-late")
        self.cloud.finish = False
        self.cloud.termination_works = False
        _, outcome = self.close()
        self.cloud.resolve_sandbox.assert_called_once()
        self.assertEqual(outcome, "cleanup_unverified")
        self.assertFalse(self.cloud.deleted)

    def test_complete_manifest_still_cannot_make_operator_close_success(self):
        self.cloud.publish(self.record)
        _, outcome = self.close(assert_no_sandbox=True)
        self.assertEqual(outcome, "error")
        self.assertTrue(self.cloud.deleted)

    def test_assertion_cannot_substitute_for_volume_ownership_or_complete_uploaded_inputs(self):
        original = dict(self.cloud.data)
        for missing, partial in (("owner.json", False), ("owner.json", True), ("run.json", False)):
            with self.subTest(missing=missing, partial=partial):
                save(self.folder / "modal-job.json", self.record)
                self.cloud.data = {
                    name: value for name, value in original.items() if name != missing
                }
                _, outcome = self.close(assert_no_sandbox=True, accept_partial=partial)
                self.assertEqual(outcome, "storage_retained")
                self.assertFalse(self.cloud.deleted)
