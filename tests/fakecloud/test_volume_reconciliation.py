"""Only an acknowledged exclusive Volume create can be reconciled by its recorded name."""

from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from podvigil.modal_backend import runner, state
from podvigil.records import save
from tests.support.modal import FakeModal, job


class VolumeReconciliation(unittest.TestCase):
    def scenario(self, *, acknowledged=True, lookup="vo-test", contents="owned"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        record = job(folder)
        record["volume"].update(state="unknown", create_acknowledged=acknowledged)
        uploads = {
            "owner.json": json.dumps(
                {"job_id": record["job_id"], "owner": record["owner"]}
            ).encode(),
            "run.json": b"{}",
        }
        record["upload_manifest"] = {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in uploads.items()
        }
        save(folder / "modal-job.json", record)
        cloud = FakeModal()
        cloud.data = dict(uploads) if contents != "empty" else {}
        if contents == "foreign":
            cloud.data["owner.json"] = b'{"owner":"another job"}'
        elif contents == "missing_owner":
            cloud.data.pop("owner.json")
            cloud.data["unexpected"] = b"unowned"

        def resolve(current):
            cloud.call("resolve_volume")
            self.assertEqual(current["volume"]["name"], record["volume"]["name"])
            if isinstance(lookup, Exception):
                raise lookup
            return lookup

        cloud.resolve_volume = resolve
        result, outcome = runner.recover(folder, provider=cloud)
        self.assertNotIn("create_volume", cloud.calls)
        self.assertNotIn("create_sandbox", cloud.calls)
        return record, state.load(folder), result, outcome, cloud

    def test_acknowledged_lookup_hit_runs_normal_owned_or_empty_recovery(self):
        for contents in ("owned", "empty"):
            with self.subTest(contents=contents):
                _, record, result, outcome, cloud = self.scenario(contents=contents)
                self.assertEqual(cloud.calls.count("resolve_volume"), 1)
                self.assertEqual(result["volume"]["id"], "vo-test")
                self.assertIn("acknowledged create", record["volume"]["evidence"])
                self.assertEqual(outcome, "error")  # Pre-compute cleanup is not workload success.
                self.assertTrue(cloud.deleted)
                self.assertTrue(
                    Path(record["backup"]["path"]).with_suffix(".verification.json").is_file()
                )

    def test_miss_error_and_unacknowledged_create_stay_unknown(self):
        for acknowledged, lookup in (
            (True, None),
            (True, TimeoutError("private")),
            (False, "vo-test"),
        ):
            with self.subTest(acknowledged=acknowledged, lookup=lookup):
                before, record, _, outcome, cloud = self.scenario(
                    acknowledged=acknowledged, lookup=lookup
                )
                self.assertEqual(record["volume"], before["volume"])
                self.assertEqual(outcome, "ambiguous")
                self.assertEqual(cloud.calls.count("resolve_volume"), int(acknowledged))
                self.assertFalse(cloud.deleted)

    def test_lookup_hit_never_bypasses_ownership_or_uploaded_inventory(self):
        for contents in ("foreign", "missing_owner"):
            with self.subTest(contents=contents):
                _, _, result, outcome, cloud = self.scenario(contents=contents)
                self.assertEqual(result["volume"]["state"], "exists")
                self.assertEqual(outcome, "storage_retained")
                self.assertFalse(cloud.deleted)
