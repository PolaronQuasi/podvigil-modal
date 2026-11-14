"""Separate startup/run windows and the unchanged legacy budget, entirely offline."""

from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile
import time
import unittest

from podvigil.modal_backend import provider, runner, runtime, spec, state
from tests.support.modal import FakeModal, job, spec as job_spec


class StartupAllowance(unittest.TestCase):
    def test_optional_budget_is_validated_and_cost_uses_run_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            config = job_spec()
            config.update(inputs={}, startup_timeout_seconds=600)
            path.write_text(json.dumps(config))
            plan = spec.plan(path)
            self.assertEqual(plan["startup_allowance_seconds"], 600)
            self.assertEqual(plan["total_allowance_seconds"], 690)
            self.assertAlmostEqual(plan["estimated_compute_usd"], 90 / 3600)
            for invalid in (True, 0, -1, 0.5, 86401, "60"):
                path.write_text(json.dumps({**config, "startup_timeout_seconds": invalid}))
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    spec.read_spec(path)

    def test_launch_deadline_is_fixed_once_and_legacy_is_untouched(self):
        for has_start in (False, True):
            record = {"config": job_spec()}
            if has_start:
                record["config"]["startup_timeout_seconds"] = 60
            runner.set_start_deadline(record, 100)
            before = dict(record)
            runner.set_start_deadline(record, 500)
            self.assertEqual(record, before)
            if has_start:
                self.assertEqual(record["startup_deadline"], 160)
                self.assertEqual(record["deadline"], 250)
            else:
                self.assertNotIn("deadline", record)

    def test_supervisor_gets_only_run_allowance_on_early_start(self):
        record = {"config": {"timeout_seconds": 90}, "startup_deadline": 700, "deadline": 790}
        self.assertEqual(runtime.runtime_window(record, 110, 10), 100)
        self.assertEqual(runtime.runtime_window(record, 699, 10), 100)
        self.assertIsNone(runtime.runtime_window(record, 700, 10))
        record.pop("startup_deadline")
        self.assertEqual(runtime.runtime_window(record, 750, 10), 50)

    def test_provider_timeout_uses_fixed_deadline_in_both_modes_and_caps_at_one_day(self):
        for startup in (False, True):
            for seconds_left, expected in ((40.9, 40), (650.5, 650), (90000, 86400)):
                with self.subTest(startup=startup, remaining=seconds_left):
                    config = job_spec()
                    if startup:
                        config["startup_timeout_seconds"] = 600
                    with (
                        patch.object(provider, "sdk", return_value=Mock()),
                        patch.object(
                            provider, "bounded_create", return_value=Mock(object_id="sb-test")
                        ) as create,
                        patch.object(provider.time, "time", return_value=100),
                    ):
                        cloud = provider.Provider(config)
                        cloud.prepare()
                        cloud.create_sandbox(
                            {
                                "job_id": "test",
                                "owner": "owner",
                                "deadline": 100 + seconds_left,
                                "volume": {"id": "vo-test"},
                                "sandbox": {"name": "test"},
                            },
                            30,
                        )
                    self.assertEqual(create.call_args.args[1], 30)  # Scheduling wait is unchanged.
                    self.assertEqual(create.call_args.kwargs["timeout"], expected)

    def test_provider_samples_deadline_after_volume_lookup(self):
        now = [100]
        with (
            patch.object(provider, "sdk", return_value=Mock()),
            patch.object(
                provider, "bounded_create", return_value=Mock(object_id="sb-test")
            ) as create,
            patch.object(provider.time, "time", side_effect=lambda: now[0]),
        ):
            cloud = provider.Provider(job_spec())
            cloud.prepare()

            def volume(_):
                now[0] = 110
                return Mock()

            cloud.volume = volume
            cloud.create_sandbox(
                {
                    "job_id": "test",
                    "owner": "owner",
                    "deadline": 160.9,
                    "volume": {"id": "vo-test"},
                    "sandbox": {"name": "test"},
                },
                30,
            )
        self.assertEqual(create.call_args.kwargs["timeout"], 50)

    def test_provider_refuses_creation_without_a_whole_second(self):
        from podvigil.modal_backend.errors import CreateNotSent

        for seconds_left in (0.9, 0, -1):
            with (
                self.subTest(remaining=seconds_left),
                patch.object(provider, "sdk", return_value=Mock()),
                patch.object(provider, "bounded_create") as create,
                patch.object(provider.time, "time", return_value=100),
            ):
                cloud = provider.Provider(job_spec())
                cloud.prepare()
                record = {"deadline": 100 + seconds_left, "volume": {"id": "vo-test"}}
                with self.assertRaisesRegex(CreateNotSent, "no whole second"):
                    cloud.create_sandbox(record, 30)
                create.assert_not_called()

    def test_expired_overall_window_changes_only_start_protocol_jobs(self):
        record = {"deadline": 100, "config": {"timeout_seconds": 30}}
        self.assertEqual(runtime.runtime_window(record, 101, 10), 10)
        record["start_protocol"] = 1
        self.assertIsNone(runtime.runtime_window(record, 100, 10))
        self.assertIsNone(runtime.runtime_window(record, 101, 10))
        self.assertEqual(runtime.runtime_window(record, 99, 10), 11)

    def test_worker_preserves_preexisting_launch_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = job_spec()
            config["startup_timeout_seconds"] = 600
            record = job(folder, config)
            now = time.time()
            runner.set_start_deadline(record, now - 10)
            from podvigil.records import save

            save(folder / "modal-job.json", record)
            cloud = FakeModal()
            runner.execute(folder, cloud, clock=lambda: now, sleep=lambda _: None)
            result = state.load(folder)
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(result["deadline"], record["deadline"])
            self.assertEqual(cloud.timeout, 590)
