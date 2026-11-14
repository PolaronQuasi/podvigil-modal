"""Offline Modal spec, command and SDK-boundary tests."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import inspect
import asyncio
import io
import json
import tempfile
import unittest

from podvigil import settings
from podvigil.cli import run_cli
from podvigil.modal_backend import SDK_VERSION
from podvigil.modal_backend.provider import Provider
from podvigil.modal_backend.spec import check_quote, portable_path, read_spec
from podvigil.modal_backend.state import find_job
from podvigil.records import save
from tests.support.modal import job, spec


class ModalSpec(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "job.json"
        self.raw = spec()
        self.raw["inputs"] = {}

    def read(self):
        self.path.write_text(json.dumps(self.raw))
        return read_spec(self.path)

    def test_offline_plan_creates_no_state_or_sdk(self):
        self.read()
        stdout = io.StringIO()
        with (
            patch(
                "podvigil.modal_backend.provider.sdk", side_effect=AssertionError("SDK forbidden")
            ),
            redirect_stdout(stdout),
        ):
            code = run_cli(["--state-dir", str(self.root / "state"), "plan", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["cloud_calls"], 0)
        self.assertFalse((self.root / "state").exists())

    def test_nonfinite_and_bool_limits_refused(self):
        for field, value in (
            ("timeout_seconds", float("nan")),
            ("cpu", True),
            ("memory_mib", 1.5),
            ("max_compute_cost_usd", float("inf")),
        ):
            with self.subTest(field=field):
                raw = self.raw.copy()
                self.raw[field] = value
                with self.assertRaises(ValueError):
                    self.read()
                self.raw = raw

    def test_spending_estimate_over_budget_refused(self):
        self.raw["max_compute_cost_usd"] = 0.0001
        with self.assertRaises(ValueError):
            self.read()

    def test_unknown_fields_and_wrong_provider_refused(self):
        for change in ({"secrets": ["account"]}, {"provider": "other"}, {"gpu": ["L4", "A10"]}):
            old = self.raw.copy()
            self.raw.update(change)
            with self.assertRaises(ValueError):
                self.read()
            self.raw = old

    def test_stale_or_future_quote_refused(self):
        config, _ = self.read()
        with self.assertRaises(ValueError):
            check_quote(config, 0)
        with self.assertRaises(ValueError):
            check_quote(config, 9999999999)

    def test_credential_files_refused(self):
        for name in (".env", ".env.local", ".modal.toml", "credentials.env", "private.key"):
            (self.root / name).write_text("test")
            self.raw["inputs"] = {"data": name}
            with self.assertRaises(ValueError):
                self.read()

    def test_file_inputs_are_explicit_and_paths_portable(self):
        (self.root / "train.py").write_text("pass")
        self.raw["inputs"] = {"code/train.py": "train.py"}
        config, sources = self.read()
        self.assertEqual(config["inputs"], ["code/train.py"])
        self.assertEqual(sources["code/train.py"], self.root / "train.py")

    def test_unsafe_artifact_paths(self):
        for name in (
            "../outside",
            "/absolute",
            "a//b",
            "C:/file",
            "a\\b",
            "NUL.txt",
            "file.",
            "./file",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                portable_path(name)

    def test_case_aliases_and_file_parent_conflicts(self):
        (self.root / "train.py").write_text("pass")
        for inputs in (
            {"File": "train.py", "file": "train.py"},
            {"code": "train.py", "code/file": "train.py"},
        ):
            self.raw["inputs"] = inputs
            with self.assertRaises(ValueError):
                self.read()

    def test_status_and_wait_need_no_sdk_and_write_nothing(self):
        folder = self.root / "ledger"
        record = job(folder)
        record.update(phase="finished", outcome="workload_failed")
        save(folder / "modal-job.json", record)
        before = (folder / "modal-job.json").read_bytes()
        for command, expected in (("status", 0), ("wait", 2)):
            out = io.StringIO()
            with (
                patch(
                    "podvigil.modal_backend.provider.sdk",
                    side_effect=AssertionError("SDK forbidden"),
                ),
                redirect_stdout(out),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(run_cli([command, str(folder)]), expected)
            self.assertEqual(json.loads(out.getvalue())["job_outcome"], "workload_failed")
        self.assertEqual(before, (folder / "modal-job.json").read_bytes())

    def test_wait_interrupt_does_not_cancel(self):
        folder = self.root / "ledger"
        job(folder)
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("podvigil.modal_backend.state.wait", side_effect=KeyboardInterrupt),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(run_cli(["wait", str(folder)]), 130)
        self.assertIn("job is unaffected", err.getvalue())
        self.assertFalse((folder / "cancel.json").exists())

    def test_modal_latest_and_prefix_resolve_only_modal_ledgers(self):
        settings.configure(state_dir=self.root)
        self.addCleanup(settings.configure)
        folder = self.root / "modal/jobs" / ("a" * 32)
        job(folder)
        self.assertEqual(find_job("latest"), folder.resolve())
        self.assertEqual(find_job("aaaa"), folder.resolve())


class ModalProvider(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        bounded = patch(
            "podvigil.modal_backend.provider.bounded_create",
            side_effect=lambda create, allowance, *args, **kwargs: create(*args, **kwargs),
        )
        bounded.start()
        self.addCleanup(bounded.stop)
        with patch("podvigil.modal_backend.provider.sdk", return_value=self.sdk):
            self.provider = Provider(spec())
        self.job = {
            "job_id": "job",
            "owner": "owner",
            "sandbox": {"id": "sb-owned", "name": "test"},
            "volume": {"id": "vo-owned"},
        }

    def test_volume_creation_is_exclusive_and_v2(self):
        self.provider.create_volume("unique")
        self.sdk.Volume.objects.create.assert_called_once_with(
            "unique", version=2, allow_existing=False, environment_name="test"
        )

    def test_credential_proof_accepts_official_failover_only(self):
        from podvigil.modal_backend.provider import credential_proof

        values = {
            "token_id": "test-id",
            "token_secret": "test-secret",
            "server_url": "https://api.modal.com,https://api.modal2.com",
        }
        config_module = SimpleNamespace(config=SimpleNamespace(get=values.get))
        with (
            patch("podvigil.modal_backend.provider.sdk"),
            patch.dict("sys.modules", {"modal.config": config_module}),
        ):
            self.assertEqual(len(credential_proof("salt")["modal"]), 64)
            for endpoint in (
                "http://api.modal.com",
                "https://api.modal.com.evil.invalid",
                "https://api.modal.com,https://evil.invalid",
                "",
            ):
                values["server_url"] = endpoint
                with self.assertRaises(RuntimeError):
                    credential_proof("salt")

    def test_volume_deletion_uses_id_not_name(self):
        self.provider.delete_volume("vo-owned")
        self.sdk.Volume.from_id.assert_called_once_with("vo-owned")
        self.sdk.Volume.from_id.return_value._instance_delete.assert_called_once_with()
        self.sdk.Volume.objects.delete.assert_not_called()

    def test_owned_sandbox_tag_check_precedes_termination(self):
        self.sdk.Sandbox.from_id.return_value.get_tags.return_value = {
            "podvigil_job": "someone-else"
        }
        with self.assertRaises(RuntimeError):
            self.provider.terminate(self.job)
        self.sdk.Sandbox.from_id.return_value.terminate.assert_not_called()

    def test_sandbox_has_bounded_resources_no_secrets_or_oidc(self):
        self.provider.prepare()
        self.provider.create_sandbox(self.job, 40)
        kwargs = self.sdk.Sandbox.create.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 40)
        self.assertEqual(kwargs["secrets"], [])
        self.assertFalse(kwargs["include_oidc_identity_token"])
        self.assertEqual(kwargs["cpu"], (1, 1))
        self.assertEqual(kwargs["memory"], (1024, 1024))
        self.assertEqual(kwargs["tags"]["podvigil_owner"], "owner")

    def test_pinned_sdk_surface_without_network(self):
        try:
            from podvigil.modal_backend.provider import sdk

            modal = sdk()
        except RuntimeError:
            self.skipTest("Install the pinned Modal dependency for the SDK contract lane.")
        from importlib.metadata import version

        self.assertEqual(version("modal"), SDK_VERSION)
        # Bind the actual installed SDK signatures; no method invocation or authentication.
        self.provider.prepare()
        self.provider.create_sandbox(self.job, 30)
        args, kwargs = self.sdk.Sandbox.create.call_args
        inspect.signature(modal.Sandbox.create).bind(*args, **kwargs)
        signature = inspect.signature(modal.Volume.objects.create)
        # Synchronicity preserves the underlying manager method's self parameter in metadata.
        prefix = [None] if next(iter(signature.parameters)) == "self" else []
        signature.bind(*prefix, "test", version=2, allow_existing=False, environment_name="test")
        self.assertTrue(callable(getattr(modal.Volume, "_instance_delete", None)))
        self.assertTrue(callable(getattr(modal.Sandbox, "get_tags", None)))
        inspect.signature(modal.Sandbox.from_id.aio).bind("sb-test")
        inspect.signature(modal.Sandbox.get_tags.aio).bind(None)


class ModalCreateDeadline(unittest.TestCase):
    def test_sdk_scheduling_is_cancelled_at_allowance(self):
        from podvigil.modal_backend.provider import bounded_create

        cancelled = []

        async def create():
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

        with self.assertRaises(asyncio.TimeoutError):
            bounded_create(SimpleNamespace(aio=create), 0.01)
        self.assertEqual(cancelled, [True])
