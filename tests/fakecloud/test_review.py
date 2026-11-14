"""Review regressions: uncertain creates, outages, cancellation and portable recovery."""

from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import time
import unittest

from podvigil.modal_backend import recovery, runner, state
from podvigil.modal_backend.errors import CreateNotSent, LifecycleError
from podvigil.records import save
from tests.support.modal import FakeModal, job


class ReviewLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = job(self.folder)
        self.provider = FakeModal()
        self.now = time.time()

    def advance(self, seconds):
        self.now += seconds

    def run_job(self):
        runner.execute(self.folder, self.provider, clock=lambda: self.now, sleep=self.advance)
        return state.load(self.folder)

    def enable_startup_allowance(self):
        self.job["config"]["startup_timeout_seconds"] = 6
        save(self.folder / "modal-job.json", self.job)
        self.provider.finish = False
        return self.now

    def test_start_receipt_outage_does_not_stop_healthy_work(self):
        started = self.enable_startup_allowance()
        observed_errors = []

        def hook(name):
            if name == "runtime_info" and self.now < started + 8:
                raise OSError("secret SDK text")
            if name == "poll":
                observed_errors.append(state.status(self.folder).get("runtime_start_error"))
                self.provider.finish = self.now >= started + 14

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        self.assertTrue(any(observed_errors))
        self.assertIsNone(result["runtime_start_error"])
        self.assertNotIn("secret SDK text", json.dumps(result))
        self.assertNotIn("terminate", self.provider.calls)
        self.assertEqual(result["deadline"], started + 96)
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)

    def test_start_receipt_outage_still_stops_at_fixed_deadline(self):
        started = self.enable_startup_allowance()
        terminated = []

        def hook(name):
            if name == "runtime_info":
                raise OSError("unavailable")
            if name == "terminate":
                terminated.append(self.now)

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(terminated, [started + 96])
        self.assertEqual(result["outcome"], "storage_retained")
        self.assertTrue(result["runtime_start_error"])
        self.assertFalse(self.provider.deleted)

    def test_cancellation_during_start_receipt_outage_still_stops_compute(self):
        started = self.enable_startup_allowance()

        def hook(name):
            if name == "runtime_info":
                raise OSError("unavailable")
            if name == "poll" and self.now >= started + 8:
                save(self.folder / "cancel.json", {})

        self.provider.hook = hook
        result = self.run_job()
        self.assertTrue(result["cancel_requested"])
        self.assertIn("terminate", self.provider.calls)
        self.assertLess(self.now, result["deadline"])

    def test_start_deadline_rechecks_an_earlier_missing_receipt(self):
        started = self.enable_startup_allowance()
        original = self.provider.runtime_info

        def receipt(record):
            found = original(record)
            return None if self.now < started + 5 else found

        def hook(name):
            if name == "create_sandbox":
                self.advance(2)
            if name == "poll":
                self.provider.finish = self.now >= started + 10

        self.provider.runtime_info = receipt
        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        self.assertNotIn("terminate", self.provider.calls)

    def test_confirmed_missing_start_receipt_stops_at_start_deadline(self):
        started = self.enable_startup_allowance()
        terminated = []

        def hook(name):
            if name == "runtime_info":
                self.provider.data.pop("runtime-start.json", None)
            if name == "terminate":
                terminated.append(self.now)

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(terminated, [started + 6])
        self.assertEqual(result["outcome"], "no_capacity")
        self.assertEqual(result["terminal_observations"], 2)
        self.assertTrue(self.provider.deleted)

    def test_start_receipt_ownership_refusal_is_not_retried(self):
        self.enable_startup_allowance()

        def hook(name):
            if name == "runtime_info":
                raise LifecycleError("Runtime start ownership does not match this job.")

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(self.provider.calls.count("runtime_info"), 1)
        self.assertIn("ownership", result["error"])
        self.assertIn("terminate", self.provider.calls)
        self.assertFalse(self.provider.deleted)

    def test_transient_poll_failures_do_not_terminate_a_healthy_job(self):
        remaining = [3]

        def hook(name):
            if name == "poll" and remaining[0]:
                remaining[0] -= 1
                raise OSError("secret SDK text")

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        self.assertIsNone(result["observation_error"])
        self.assertEqual(result["sandbox_exit_code"], 0)
        self.assertNotIn("terminate", self.provider.calls)
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)

    def test_ownership_failure_is_not_retried_as_a_transport_outage(self):
        def hook(name):
            if name == "poll":
                raise LifecycleError("Sandbox ownership tags do not match its creation receipt.")

        self.provider.hook = hook
        result = self.run_job()
        self.assertEqual(result["failed_phase"], "running")
        self.assertIn("ownership", result["error"])
        self.assertNotIn("delete_volume", self.provider.calls)
        self.assertLess(self.now, result["deadline"])

    def test_cancel_during_poll_outage_still_attempts_stop(self):
        def hook(name):
            if name == "poll":
                save(self.folder / "cancel.json", {})
                raise OSError("unavailable")

        self.provider.hook = hook
        result = self.run_job()
        self.assertTrue(result["cancel_requested"])
        self.assertIn("terminate", self.provider.calls)
        self.assertLess(self.now, result["deadline"])
        self.assertFalse(self.provider.deleted)

    def test_cancel_allows_supervisor_to_publish_before_hard_stop(self):
        self.provider.finish = False
        self.provider.hook = lambda name: (
            save(self.folder / "cancel.json", {}) if name == "poll" else None
        )

        def request_stop(record):
            self.provider.call("request_stop")
            self.provider.training_outcome = "cancelled"
            self.provider.publish(record)
            self.provider.running = False

        self.provider.request_stop = request_stop
        result = self.run_job()
        self.assertEqual(result["outcome"], "cancelled")
        self.assertTrue(self.provider.deleted)
        self.assertNotIn("terminate", self.provider.calls)

    def test_graceful_cancel_has_a_deadline_then_hard_termination(self):
        self.provider.finish = False
        self.provider.request_stop = lambda record: self.provider.call("request_stop")
        self.provider.hook = lambda name: (
            save(self.folder / "cancel.json", {}) if name == "poll" else None
        )
        result = self.run_job()
        self.assertIn("request_stop", self.provider.calls)
        self.assertIn("terminate", self.provider.calls)
        self.assertLessEqual(self.now, result["deadline"])

    def test_cancel_after_upload_verifies_and_removes_inputs_only_volume(self):
        self.provider.hook = lambda name: (
            save(self.folder / "cancel.json", {}) if name == "upload" else None
        )
        result = self.run_job()
        self.assertEqual(result["outcome"], "cancelled")
        self.assertTrue(result["unstarted_backup"])
        self.assertTrue(self.provider.deleted)
        self.assertNotIn("create_sandbox", self.provider.calls)
        self.assertEqual(recovery.completed_outcome(result), "cancelled")

    def test_injected_pre_sdk_failure_keeps_defensive_cleanup_rule(self):
        # Defensive seam only: a normal successful upload already caches the Volume handle.
        def create(record, remaining):
            raise CreateNotSent("Sandbox creation was not sent.")

        self.provider.create_sandbox = create
        result = self.run_job()
        self.assertEqual(result["sandbox"]["state"], "not_created")
        self.assertEqual(result["outcome"], "error")
        self.assertTrue(self.provider.deleted)
        self.assertEqual(recovery.completed_outcome(result), "error")

    def test_unstarted_cleanup_rejects_extra_or_modified_uploads(self):
        for name, value in (("unexpected", b"new"), ("run.json", b"changed")):
            with self.subTest(name=name):
                self.provider = FakeModal()
                job(self.folder)
                self.now = time.time()

                def hook(event):
                    if event == "upload":
                        self.now += 200
                    if event == "files":
                        self.provider.data[name] = value

                self.provider.hook = hook
                result = self.run_job()
                self.assertEqual(result["outcome"], "storage_retained")
                self.assertFalse(self.provider.deleted)

    def test_unknown_create_never_uses_unstarted_cleanup(self):
        self.provider.failure = "create_sandbox"
        result = self.run_job()
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertFalse(self.provider.deleted)
        self.assertNotIn("files", self.provider.calls)

    def test_recover_running_job_requires_explicit_stop_even_after_lookup(self):
        for initial in ("exists", "unknown"):
            with self.subTest(initial=initial):
                record = job(self.folder)
                record["sandbox"].update(state=initial, id="sb-test")
                record["volume"].update(state="exists", id="vo-test")
                record["deadline"] = time.time() + 60
                save(self.folder / "modal-job.json", record)
                self.provider.finish = False
                self.provider.resolve_sandbox = lambda record: "sb-test"
                result, outcome = runner.recover(self.folder, provider=self.provider)
                self.assertEqual(outcome, "cleanup_unverified")
                self.assertIn("--stop-running", result["error"])
                self.assertNotIn("terminate", self.provider.calls)
                saved = state.load(self.folder)
                self.assertEqual(saved["phase"], record["phase"])
                self.assertEqual(saved.get("outcome"), record.get("outcome"))
                self.assertFalse(state.status(self.folder)["ended"])
                self.assertEqual(state.wait(self.folder, timeout=0)[1], "wait_timeout")
                self.assertEqual(saved["sandbox"]["id"], "sb-test")

    def test_manual_sandbox_receipt_checks_ownership_and_conflicts(self):
        record = job(self.folder)
        record["sandbox"]["state"] = "unknown"
        save(self.folder / "modal-job.json", record)

        def sandbox(candidate):
            self.assertEqual(candidate["sandbox"]["id"], "sb-recovered")
            raise LifecycleError("ownership mismatch")

        self.provider.sandbox = sandbox
        with self.assertRaisesRegex(LifecycleError, "ownership"):
            runner.recover(self.folder, sandbox_id="sb-recovered", provider=self.provider)
        self.assertNotIn("id", state.load(self.folder)["sandbox"])

    def test_manual_sandbox_receipt_recovers_owned_stopped_compute(self):
        self.provider.failure = "create_sandbox"
        self.run_job()
        self.provider.failure = None
        self.provider.publish(state.load(self.folder))
        self.provider.running = False
        checked = []
        self.provider.sandbox = lambda candidate: checked.append(candidate["sandbox"]["id"])
        with patch.object(recovery.time, "sleep"):
            document, outcome = runner.recover(
                self.folder, sandbox_id="sb-recovered", provider=self.provider
            )
        self.assertEqual(checked, ["sb-recovered"])
        self.assertEqual(outcome, "success")
        self.assertEqual(document["sandbox"]["id"], "sb-recovered")
        self.assertEqual(self.provider.calls.count("create_sandbox"), 1)

    def test_unresolved_recovery_does_not_relookup_and_stop_a_late_running_match(self):
        record = job(self.folder)
        record["sandbox"]["state"] = "unknown"
        save(self.folder / "modal-job.json", record)
        from unittest.mock import Mock

        self.provider.resolve_sandbox = Mock(side_effect=[None, "sb-late"])
        _, outcome = runner.recover(self.folder, provider=self.provider)
        self.assertEqual(outcome, "ambiguous")
        self.provider.resolve_sandbox.assert_called_once()
        self.assertNotIn("terminate", self.provider.calls)

    def test_output_names_encode_without_aliases_and_keep_verification(self):
        names = ["epoch=1-step=100.ckpt", "Résumé.ckpt", "Case", "case", "NUL.txt", "x" * 300]
        for name in names:
            self.provider.data["outputs/" + name] = name.encode()
        result = self.run_job()
        self.assertEqual(result["outcome"], "success")
        backup = Path(result["backup"]["path"])
        mapping = json.loads((backup / "paths.json").read_text())["remote_to_local"]
        self.assertEqual(len(mapping), len(set(mapping.values())))
        for name in names:
            self.assertEqual((backup / mapping["outputs/" + name]).read_bytes(), name.encode())

    def test_parent_collision_is_detected_even_when_nonadjacent(self):
        self.provider.data = {"a": b"1", "a-b": b"2", "a/b": b"3"}
        self.job["volume"].update(state="exists", id="vo-test")
        with self.assertRaisesRegex(ValueError, "parent directory"):
            recovery.inventory(self.provider, self.job)

    def test_failed_backups_reuse_one_directory_for_same_inventory(self):
        self.provider.failure = "read:outputs/result.txt"
        result = self.run_job()
        self.assertEqual(result["outcome"], "storage_retained")
        before = [p for p in self.folder.glob("backup-*") if p.is_dir()]
        self.assertEqual(len(before), 1)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                recovery.recover_outputs(self.provider, self.folder, result, sleep=lambda _: None)
        self.assertEqual(before, [p for p in self.folder.glob("backup-*") if p.is_dir()])
        self.provider.failure = None
        self.assertEqual(
            recovery.recover_outputs(self.provider, self.folder, result, sleep=lambda _: None),
            "success",
        )

    def test_changed_inventory_preserves_previous_partial_copy(self):
        self.provider.publish_manifest = False
        result = self.run_job()
        self.provider.failure = "read:outputs/result.txt"
        with self.assertRaises(RuntimeError):
            recovery.recover_outputs(
                self.provider, self.folder, result, accept_partial=True, sleep=lambda _: None
            )
        before = [p for p in self.folder.glob("backup-*") if p.is_dir()]
        self.provider.data["outputs/new-file"] = b"new"
        with self.assertRaises(RuntimeError):
            recovery.recover_outputs(
                self.provider, self.folder, result, accept_partial=True, sleep=lambda _: None
            )
        after = [p for p in self.folder.glob("backup-*") if p.is_dir()]
        self.assertEqual(len(after), 2)
        self.assertTrue(before[0].exists())

    def test_wait_detects_stalled_control_loop_despite_heartbeat_thread(self):
        self.job.update(progress_at=0, deadline=time.time() - 1)
        self.job["sandbox"]["state"] = "exists"
        save(self.folder / "modal-job.json", self.job)
        save(self.folder / "modal-heartbeat.json", {"at": time.time()})
        before = (self.folder / "modal-job.json").read_bytes()
        lines = []
        document, outcome = state.wait(self.folder, progress=lines.append)
        self.assertEqual(outcome, "cleanup_unverified")
        self.assertTrue(document["watchdog_heard"])
        self.assertFalse(document["loop_heard"])
        self.assertTrue(document["deadline_overdue"])
        self.assertIn("control loop", lines[-1])
        self.assertEqual(before, (self.folder / "modal-job.json").read_bytes())

    def test_safe_diagnostics_explain_refusal_but_never_log_sdk_message(self):
        self.provider.failure = "upload"
        self.run_job()
        diagnostic = json.loads((self.folder / "error.json").read_text())
        self.assertNotIn("simulated secret", json.dumps(diagnostic))
        self.assertTrue(diagnostic["frames"])
        for frame in diagnostic["frames"]:
            self.assertEqual(Path(frame["file"]).name, frame["file"])
        self.provider = FakeModal()
        self.provider.publish_manifest = False
        job(self.folder)
        result = self.run_job()
        self.assertIn("--accept-partial", result["error"])
