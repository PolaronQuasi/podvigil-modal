"""Offline regression checks at the pinned SDK and user-error boundaries."""

from pathlib import Path
from unittest.mock import Mock, patch
import inspect
import json
import tempfile
import time
import unittest

from podvigil.modal_backend import provider, runtime
from podvigil.modal_backend.errors import CreateNotSent, LockBusy, UserError, job_lock, safe_error
from podvigil.modal_backend.spec import check_quote, read_spec
from podvigil.records import exclusive_lock
from tests.support.modal import spec


class ReviewBoundaries(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sdk.exception.NotFoundError = type("NotFoundError", (Exception,), {})
        with patch.object(provider, "sdk", return_value=self.sdk):
            self.provider = provider.Provider(spec())
        self.job = {
            "deadline": time.time() + 90,
            "job_id": "job",
            "owner": "owner",
            "sandbox": {"id": "sb-test", "name": "test"},
            "volume": {"id": "vo-test"},
        }
        self.tags = {"podvigil_job": "job", "podvigil_owner": "owner"}

    def test_gpu_lookup_falls_back_but_does_not_call_create(self):
        sandbox = Mock(object_id="sb-test")
        sandbox.get_tags.return_value = self.tags
        self.sdk.Sandbox.from_name.side_effect = [self.sdk.exception.NotFoundError(), sandbox]
        self.assertEqual(self.provider.resolve_sandbox(self.job), "sb-test")
        self.assertEqual(
            [
                call.kwargs["include_terminated"]
                for call in self.sdk.Sandbox.from_name.call_args_list
            ],
            [True, False],
        )
        self.sdk.Sandbox.create.assert_not_called()

    def test_two_lookup_misses_remain_unresolved(self):
        self.sdk.Sandbox.from_name.side_effect = self.sdk.exception.NotFoundError
        self.assertIsNone(self.provider.resolve_sandbox(self.job))
        self.sdk.Sandbox.create.assert_not_called()

    def test_cached_reads_still_recheck_ownership_before_mutation(self):
        sandbox = self.sdk.Sandbox.from_id.return_value
        sandbox.get_tags.return_value = self.tags
        self.provider.poll(self.job)
        self.provider.poll(self.job)
        sandbox.get_tags.assert_called_once()
        sandbox.get_tags.return_value = {}
        with self.assertRaisesRegex(UserError, "ownership"):
            self.provider.terminate(self.job)
        sandbox.terminate.assert_not_called()

    def test_volume_handles_cached_but_absence_read_is_fresh(self):
        self.provider.volume("vo-test")
        self.provider.volume("vo-test")
        self.sdk.Volume.from_id.assert_called_once()
        self.sdk.Volume.from_id.side_effect = self.sdk.exception.NotFoundError
        self.assertFalse(self.provider.volume_exists("vo-test"))

    def test_volume_create_acknowledged_before_retrying_only_lookup(self):
        acknowledged = Mock()
        self.sdk.Volume.from_name.side_effect = [OSError("unavailable"), Mock(object_id="vo-test")]
        with patch.object(provider.time, "sleep"):
            self.assertEqual(
                self.provider.create_volume("unique", acknowledged=acknowledged), "vo-test"
            )
        acknowledged.assert_called_once()
        self.sdk.Volume.objects.create.assert_called_once()
        self.assertEqual(self.sdk.Volume.from_name.call_count, 2)

    def test_defensive_pre_call_dependency_failure_has_no_create_evidence_in_isolation(self):
        with patch.object(self.provider, "volume", side_effect=OSError("secret")):
            with self.assertRaises(CreateNotSent):
                self.provider.create_sandbox(self.job, 60)
        self.sdk.Sandbox.create.assert_not_called()

    def test_uploaded_volume_is_cached_so_lookup_failure_does_not_prove_create_not_sent(self):
        from unittest.mock import MagicMock

        volume = MagicMock()
        self.provider._volumes["vo-test"] = volume
        self.provider.upload("vo-test", {"owner.json": b"{}"})
        self.sdk.Volume.from_id.side_effect = OSError("lookup unavailable")
        self.provider.app, self.provider.image = Mock(), Mock()
        with patch.object(
            provider, "bounded_create", side_effect=RuntimeError("create reached")
        ) as create:
            with self.assertRaisesRegex(RuntimeError, "create reached"):
                self.provider.create_sandbox(self.job, 60)
        create.assert_called_once()

    def test_graceful_signal_requires_owned_sandbox_and_successful_exec(self):
        sandbox = self.sdk.Sandbox.from_id.return_value
        sandbox.get_tags.return_value = self.tags
        sandbox.exec.return_value.wait.return_value = 0
        self.provider.request_stop(self.job)
        self.assertEqual(sandbox.exec.call_args.kwargs["timeout"], 10)
        self.assertIn("SIGTERM", sandbox.exec.call_args.args[2])
        sandbox.exec.return_value.wait.return_value = 1
        with self.assertRaisesRegex(UserError, "hard termination"):
            self.provider.request_stop(self.job)

    def test_own_messages_visible_sdk_value_errors_are_not(self):
        self.assertEqual(
            safe_error(UserError("No manifest; use --accept-partial.")),
            "No manifest; use --accept-partial.",
        )
        self.assertNotIn("secret-token", safe_error(ValueError("secret-token")))

    def test_busy_lock_message_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with exclusive_lock(folder / "modal-worker.lock", timeout=0):
                with self.assertRaisesRegex(LockBusy, "live worker"):
                    with job_lock(folder):
                        self.fail("must not acquire")

    def test_zulu_quote_works_on_every_supported_python(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            config = spec()
            config["rate_checked_utc"] = config["rate_checked_utc"].replace("+00:00", "Z")
            config["inputs"] = {}
            path.write_text(json.dumps(config))
            validated, _ = read_spec(path)
            check_quote(validated)

    def test_inputs_cannot_exhaust_recovery_limits_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "input").write_bytes(b"1234")
            config = spec()
            config["inputs"] = {"data": "input"}
            for change in ({"max_backup_bytes": 4}, {"max_backup_files": 4}):
                path = folder / "job.json"
                path.write_text(json.dumps({**config, **change}))
                with self.assertRaisesRegex(UserError, "exhaust"):
                    read_spec(path)

    def test_final_commit_uses_whole_reserve_periodic_commit_is_bounded(self):
        with (
            patch.object(runtime.time, "monotonic", return_value=100),
            patch.object(runtime.subprocess, "run") as run,
        ):
            runtime.sync_volume(Path("volume"), 190)
            self.assertEqual(run.call_args.kwargs["timeout"], 90)
            runtime.sync_volume(Path("volume"), 190, periodic=True)
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
            with self.assertRaises(TimeoutError):
                runtime.sync_volume(Path("volume"), 99)

    def test_supervisor_failure_keeps_sanitized_diagnostic_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(runtime, "run", side_effect=RuntimeError("secret")):
                self.assertEqual(runtime.main(folder / "run.json"), 1)
                error = folder / "supervisor-error.json"
                self.assertEqual(json.loads(error.read_text()), {"error_type": "RuntimeError"})
                error.write_text("existing evidence")
                self.assertEqual(runtime.main(folder / "run.json"), 1)
                self.assertEqual(error.read_text(), "existing evidence")

    def test_extended_sdk_contract_is_offline(self):
        try:
            modal = provider.sdk()
        except RuntimeError:
            self.skipTest("Pinned SDK contract lane only")

        def bind(method, *args, **kwargs):
            signature = inspect.signature(method)
            prefix = [None] if next(iter(signature.parameters), None) == "self" else []
            signature.bind(*prefix, *args, **kwargs)

        for include in (True, False):
            bind(
                modal.Sandbox.from_name,
                "app",
                "sandbox",
                environment_name="test",
                include_terminated=include,
            )
        bind(modal.Sandbox.exec, "python3", "-c", "pass", timeout=10)
        bind(modal.Sandbox.from_id, "sb-test")
        bind(modal.Sandbox.get_tags)
        bind(modal.Sandbox.poll)
        bind(modal.Sandbox.terminate)
        bind(modal.Volume.from_id, "vo-test")
        bind(modal.Volume.from_name, "test", environment_name="test")
        bind(modal.Volume.hydrate)
        bind(modal.Volume.iterdir, "/", recursive=True)
        bind(modal.Volume.read_file, "manifest.json")
        bind(modal.Volume.batch_upload, force=False)
        bind(modal.Volume._instance_delete)
        bind(modal.Image.build, app=None)
        from modal.container_process import ContainerProcess
        from modal.io_streams import StreamReader

        bind(ContainerProcess.wait)
        bind(StreamReader.read)
