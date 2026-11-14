"""GPU samples are bounded, nonfatal and distinguish missing measurements from zero use."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import subprocess
import tempfile
import unittest
from podvigil.modal_backend import runtime


class GpuMetrics(unittest.TestCase):
    def test_initial_sample_reserves_start_margin_and_skips_when_too_late(self):
        for startup, finish_by, expected in (
            (None, 150, 10),
            (205, 150, 4.75),
            (220, 103, 2.75),
            (200.3, 150, None),
            (199, 150, None),
            (220, 100.2, None),
        ):
            with (
                self.subTest(startup=startup, finish_by=finish_by),
                patch.object(runtime.time, "time", return_value=200),
                patch.object(runtime.time, "monotonic", return_value=100),
            ):
                record = {"deadline": 230}
                if startup is not None:
                    record["startup_deadline"] = startup
                monitor = Mock()
                runtime.initial_gpu_sample(monitor, record, finish_by)
                if expected is None:
                    monitor.sample.assert_not_called()
                else:
                    monitor.sample.assert_called_once_with(timeout=expected)

    def test_long_first_sample_does_not_change_later_one_second_timeout(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runtime.shutil, "which", return_value="nvidia-smi"),
            patch.object(
                runtime.subprocess, "run", return_value=SimpleNamespace(stdout="Tesla L4, 10, 50\n")
            ) as run,
        ):
            monitor = runtime.GpuMonitor(Path(directory))
            with patch.object(runtime.time, "monotonic", return_value=10):
                monitor.sample(timeout=10)
            with patch.object(runtime.time, "monotonic", return_value=20):
                monitor.sample()
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [10, 1])

    def test_device_observations_are_aggregated_and_written(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runtime.shutil, "which", return_value="nvidia-smi"),
            patch.object(
                runtime.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(stdout="Test GPU, 20, 100\nOther GPU, 40, 200\n"),
                    SimpleNamespace(stdout="Test GPU, 60, 300\n"),
                ],
            ) as run,
        ):
            monitor = runtime.GpuMonitor(Path(directory))
            with patch.object(runtime.time, "monotonic", return_value=10):
                monitor.sample()
                monitor.sample()
            with patch.object(runtime.time, "monotonic", return_value=15):
                monitor.sample()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs["timeout"], 1)
            summary = monitor.summary()
            self.assertEqual(summary["mean_utilization_percent"], 40)
            self.assertEqual(summary["peak_utilization_percent"], 60)
            self.assertEqual(summary["peak_memory_mib"], 300)
            self.assertEqual(len(monitor.path.read_text().splitlines()), 2)

    def test_absent_gpu_tool_never_claims_zero_utilization(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runtime.shutil, "which", return_value=None),
        ):
            monitor = runtime.GpuMonitor(Path(directory))
            monitor.sample()
            self.assertIsNone(monitor.summary()["mean_utilization_percent"])
            self.assertFalse(monitor.summary()["available"])

    def test_bad_or_slow_tool_and_full_disk_are_nonfatal(self):
        for error in (
            OSError("missing"),
            subprocess.TimeoutExpired("nvidia-smi", 1),
            ValueError("malformed"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
                patch.object(runtime.shutil, "which", return_value="nvidia-smi"),
                patch.object(runtime.subprocess, "run", side_effect=error),
            ):
                monitor = runtime.GpuMonitor(Path(directory))
                monitor.sample()
                self.assertEqual(monitor.summary()["sampling_errors"], 1)
