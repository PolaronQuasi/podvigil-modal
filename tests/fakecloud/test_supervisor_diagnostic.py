"""Only the known late supervisor diagnostic may be absent from the output manifest."""

from pathlib import Path
import json
import tempfile
import unittest

from podvigil.modal_backend import recovery, runner, state
from tests.support.modal import FakeModal, job


class LateDiagnostic(unittest.TestCase):
    def test_late_record_is_verified_twice_and_does_not_override_manifest_outcome(self):
        for training, expected in (("success", "success"), ("not_started", "workload_failed")):
            with self.subTest(training=training), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                job(folder)

                class LateFailure(FakeModal):
                    def publish(self, record):
                        super().publish(record)
                        self.data["supervisor-error.json"] = b'{"error_type":"TimeoutError"}'

                cloud = LateFailure()
                cloud.training_outcome = training
                runner.execute(folder, cloud, sleep=lambda _: None)
                result = state.status(folder)
                self.assertEqual(result["job_outcome"], expected)
                self.assertTrue(cloud.deleted)
                self.assertEqual(result["supervisor_error"], {"error_type": "TimeoutError"})
                self.assertGreaterEqual(cloud.calls.count("read:supervisor-error.json"), 3)
                self.assertEqual(
                    json.loads(
                        (Path(result["backup"]["path"]) / "supervisor-error.json").read_text()
                    ),
                    result["supervisor_error"],
                )

    def test_other_extras_malformed_oversized_or_changing_diagnostics_still_retain(self):
        for case in ("extra", "malformed", "oversized", "changing", "changing_early"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                record = job(folder)
                record["sandbox"].update(state="stopped", id="sb-test")
                record["volume"].update(state="exists", id="vo-test")
                cloud = FakeModal()
                cloud.data["owner.json"] = json.dumps(
                    {"job_id": record["job_id"], "owner": record["owner"]}
                ).encode()
                cloud.publish(record)
                cloud.data["supervisor-error.json"] = b'{"error_type":"TimeoutError"}'
                if case == "extra":
                    cloud.data["unexpected.json"] = b"{}"
                elif case == "malformed":
                    cloud.data["supervisor-error.json"] = b'{"error_type":123}'
                elif case == "oversized":
                    cloud.data["supervisor-error.json"] = b" " * 65537
                else:

                    def change(name):
                        if name == "read:supervisor-error.json" and cloud.calls.count(name) == (
                            2 if case == "changing_early" else 3
                        ):
                            cloud.data["supervisor-error.json"] = b'{"error_type":"RuntimeError"}'

                    cloud.hook = change
                with self.assertRaises(ValueError):
                    recovery.recover_outputs(
                        cloud, folder, record, accept_partial=True, sleep=lambda _: None
                    )
                self.assertFalse(cloud.deleted)
