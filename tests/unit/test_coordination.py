"""Ledger ordering and exclusive-writer regressions, with no provider SDK access."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import os
import runpy
import sys
import tempfile
import unittest

from podvigil import settings
from podvigil.cli import run_cli
from podvigil.modal_backend import runner, state
from podvigil.records import exclusive_lock, save
from tests.support.modal import job, spec


class Coordination(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings.configure(state_dir=self.root)
        self.addCleanup(settings.configure)

    def test_latest_uses_creation_time_even_if_older_job_is_updated(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                older = state.jobs_dir() / ("a" * 32)
                newer = state.jobs_dir() / ("b" * 32)
                for folder, date, stamp in (
                    (older, "2026-01-01T00:00:00+00:00", 100),
                    (newer, "2026-01-02T00:00:00+00:00", 200),
                ):
                    record = job(folder)
                    if not legacy:
                        record["created_utc"] = date
                    save(folder / "modal-job.json", record)
                    save(folder / "job.json", {"provider": "modal"})
                    os.utime(folder / "job.json", (stamp, stamp))
                state.update(older, state.load(older), phase="running")
                self.assertEqual(state.find_job("latest"), newer.resolve())
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        run_cli(["--state-dir", str(self.root), "cancel", "latest"]), 11
                    )
                self.assertTrue((newer / "cancel.json").exists())
                self.assertFalse((older / "cancel.json").exists())
                settings.configure(state_dir=self.root)

    def test_rejected_worker_does_not_modify_live_owners_ledger(self):
        folder = self.root / "active"
        record = job(folder)
        record.update(phase="running", launch_started=True)
        record["sandbox"].update(state="exists", id="sb-test")
        save(folder / "modal-job.json", record)
        before = (folder / "modal-job.json").read_bytes()
        with (
            exclusive_lock(folder / "modal-worker.lock", timeout=0),
            patch.object(sys, "argv", ["worker", str(folder)]),
        ):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("podvigil.modal_backend.worker", run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual((folder / "modal-job.json").read_bytes(), before)
            self.assertFalse(state.status(folder)["ended"])

    def test_worker_reports_failure_only_while_holding_lock(self):
        folder = self.root / "failed"
        job(folder)
        original_update = runner.best_effort_update

        def update(*args, **kwargs):
            with self.assertRaises(TimeoutError):
                with exclusive_lock(folder / "modal-worker.lock", timeout=0):
                    pass
            return original_update(*args, **kwargs)

        with (
            patch.object(runner, "_owned_worker", side_effect=RuntimeError("private details")),
            patch.object(runner, "best_effort_update", side_effect=update),
        ):
            with self.assertRaises(RuntimeError):
                runner.worker(folder)
        self.assertEqual(state.load(folder)["outcome"], "error")
        self.assertNotIn("private details", state.load(folder)["error"])

    def test_interrupted_release_preserves_worker_receipts_with_or_without_live_owner(self):
        for owner_live in (False, True):
            with self.subTest(owner_live=owner_live), ExitStack() as stack:

                def interrupted_save(path, value):
                    save(path, value)
                    if path.name == "release.json":
                        record = state.load(path.parent)
                        record.update(phase="running", launch_started=True)
                        record["sandbox"].update(state="exists", id="sb-test")
                        record["volume"].update(state="exists", id="vo-test")
                        state.update(path.parent, record)
                        if owner_live:
                            stack.enter_context(
                                exclusive_lock(path.parent / "modal-worker.lock", timeout=0)
                            )
                        raise KeyboardInterrupt

                with (
                    patch.object(runner, "read_spec", return_value=(spec(), {})),
                    patch.object(runner, "credential_proof", return_value={"modal": "dummy"}),
                    patch.object(runner, "spawn", return_value=SimpleNamespace(close=lambda: None)),
                    patch.object(runner, "save", side_effect=interrupted_save),
                ):
                    result, outcome = runner.launch("unused", detach=True)
                self.assertEqual(outcome, "interrupted")
                self.assertEqual(result["sandbox"]["id"], "sb-test")
                self.assertEqual(result["volume"]["id"], "vo-test")
                self.assertFalse(result["ended"])
                self.assertTrue((Path(result["ledger"]) / "cancel.json").exists())

    def test_failed_launch_before_release_finishes_without_allocating(self):
        with (
            patch.object(runner, "read_spec", return_value=(spec(), {})),
            patch.object(runner, "credential_proof", return_value={"modal": "dummy"}),
            patch.object(runner, "spawn", side_effect=RuntimeError("arming failed")),
        ):
            result, outcome = runner.launch("unused")
        self.assertEqual(outcome, "error")
        self.assertTrue(result["ended"])
        self.assertEqual(result["sandbox"]["state"], "not_created")
