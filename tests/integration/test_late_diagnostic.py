"""Recover real entrypoint output after failed final sync, without Modal or a network."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.modal_backend import recovery, runtime, state
from tests.support.directory_modal import DirectoryModal
from tests.support.modal import job


@unittest.skipUnless(sys.platform.startswith("linux"), "Sandbox runtime requires Linux")
class EntrypointRecovery(unittest.TestCase):
    def test_expired_deadline_and_failed_final_sync_recover_the_manifest_outcome(self):
        for expired in (False, True):
            with self.subTest(expired=expired), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                root = folder / "volume"
                (root / "inputs").mkdir(parents=True)
                ledger = folder / "ledger"
                record = job(ledger)
                record["config"].update(
                    command=[sys.executable, "train.py"], timeout_seconds=30, finalize_seconds=5
                )
                record["deadline"] = time.time() + (-5 if expired else 30)
                (root / "inputs/train.py").write_text(
                    "import os,pathlib;pathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'],'result.txt').write_text('ok')"
                )
                (root / "run.json").write_text(json.dumps(record))
                (root / "owner.json").write_text(
                    json.dumps({"job_id": record["job_id"], "owner": record["owner"]})
                )
                binary = folder / "bin"
                binary.mkdir()
                (binary / "sync").write_text("#!/bin/sh\nexit 1\n")
                (binary / "sync").chmod(0o700)
                result = subprocess.run(
                    [sys.executable, "-B", runtime.__file__, str(root / "run.json")],
                    env=dict(
                        os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", "")
                    ),
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 1)
                self.assertTrue((root / "supervisor-error.json").exists())
                record["sandbox"].update(state="exists", id="sb-test")
                record["volume"].update(state="exists", id="vo-test")
                cloud = DirectoryModal(root, result.returncode)
                outcome = recovery.recover_outputs(cloud, ledger, record, sleep=lambda _: None)
                self.assertEqual(outcome, "workload_failed" if expired else "success")
                self.assertTrue(cloud.deleted)
                self.assertGreaterEqual(cloud.polls, 2)
                self.assertGreaterEqual(cloud.reads.count("supervisor-error.json"), 3)
                self.assertIsNotNone(state.status(ledger)["supervisor_error"])
