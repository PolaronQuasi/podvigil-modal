"""No-capacity cleanup depends on positive protocol/ownership/backup evidence."""

from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import tempfile
import time
import unittest

from podvigil.modal_backend import recovery, runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class NoCapacity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.record = job(self.folder)
        self.record.update(start_protocol=1, terminal_observations=2)
        self.record["sandbox"].update(state="stopped", id="sb-test")
        self.record["volume"].update(state="exists", id="vo-test")
        self.cloud = FakeModal()
        self.cloud.running = False
        self.cloud.data = {
            "owner.json": json.dumps(
                {"job_id": self.record["job_id"], "owner": self.record["owner"]}
            ).encode(),
            "run.json": b'{"start_protocol":1}',
            "inputs/train.py": b"pass",
        }
        self.record["upload_manifest"] = {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in self.cloud.data.items()
        }
        save(self.folder / "modal-job.json", self.record)

    def test_exact_inputs_only_get_verified_backup_and_code_four(self):
        self.assertEqual(
            recovery.recover_outputs(self.cloud, self.folder, self.record, sleep=lambda _: None),
            "no_capacity",
        )
        self.assertTrue(self.cloud.deleted)
        self.assertEqual(recovery.completed_outcome(self.record), "no_capacity")
        self.assertEqual(
            (Path(self.record["backup"]["path"]) / "inputs/train.py").read_bytes(), b"pass"
        )

    def test_plain_recover_reaches_the_same_outcome(self):
        document, outcome = runner.recover(self.folder, provider=self.cloud)
        self.assertEqual(outcome, "no_capacity")
        self.assertEqual(document["volume"]["state"], "deleted")

    def test_missing_protocol_or_second_observation_never_qualifies(self):
        for key in ("start_protocol", "terminal_observations"):
            with self.subTest(key=key):
                record = {**self.record}
                record.pop(key)
                with self.assertRaisesRegex(ValueError, "manifest"):
                    recovery.recover_outputs(self.cloud, self.folder, record, sleep=lambda _: None)
                self.assertFalse(self.cloud.deleted)

    def test_start_gate_or_extra_file_retains_storage(self):
        for name in ("runtime-start.json", "outputs/checkpoint"):
            with self.subTest(name=name):
                self.cloud.data[name] = b"evidence"
                with self.assertRaises(ValueError):
                    recovery.recover_outputs(
                        self.cloud, self.folder, self.record, sleep=lambda _: None
                    )
                self.cloud.data.pop(name)
                self.assertFalse(self.cloud.deleted)

    def test_changed_upload_never_deleted_even_without_start_marker(self):
        self.cloud.data["inputs/train.py"] = b"evil"
        with self.assertRaisesRegex(ValueError, "bytes"):
            recovery.recover_outputs(self.cloud, self.folder, self.record, sleep=lambda _: None)
        self.assertFalse(self.cloud.deleted)

    def test_lost_create_reply_resolved_and_terminal_twice_can_clean(self):
        job(self.folder)
        cloud = FakeModal()
        cloud.finish = False
        cloud.resolve_sandbox = lambda record: "sb-test"

        def create(record, remaining):
            cloud.call("create_sandbox")
            raise TimeoutError("not absence evidence")

        cloud.create_sandbox = create
        runner.execute(self.folder, cloud, sleep=lambda _: None)
        record = state.load(self.folder)
        self.assertEqual(record["outcome"], "no_capacity")
        self.assertEqual(record["terminal_observations"], 2)
        self.assertEqual(cloud.calls.count("create_sandbox"), 1)
        self.assertTrue(cloud.deleted)

    def test_unresolved_create_is_never_capacity_evidence(self):
        self.record["sandbox"]["state"] = "unknown"
        with self.assertRaises(RuntimeError):
            recovery.recover_outputs(self.cloud, self.folder, self.record, sleep=lambda _: None)
        self.assertFalse(self.cloud.deleted)

    def test_start_allowance_expiring_before_create_needs_no_terminal_inference(self):
        for boundary in ("build_image", "upload"):
            with self.subTest(boundary=boundary):
                config = self.record["config"].copy()
                config["startup_timeout_seconds"] = 5
                job(self.folder, config)
                cloud = FakeModal()
                now = [time.time()]

                def hook(name):
                    if name == boundary:
                        now[0] += 6

                cloud.hook = hook
                runner.execute(self.folder, cloud, clock=lambda: now[0], sleep=lambda _: None)
                record = state.load(self.folder)
                self.assertEqual(record["outcome"], "no_capacity")
                self.assertNotIn("create_sandbox", cloud.calls)
                self.assertTrue(record["no_workload_evidence"]["sandbox_create_not_sent"])
                self.assertEqual(cloud.deleted, boundary == "upload")
