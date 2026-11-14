"""Exercise actual supervisor signals and output publication on local Linux."""

from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.modal_backend import runtime


@unittest.skipUnless(sys.platform.startswith("linux"), "Real Linux supervisor signals")
class SupervisorSignals(unittest.TestCase):
    def test_sigterm_finalizes_checkpoint_and_reaps_detached_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inputs").mkdir()
            (root / "inputs/train.py").write_text(
                "import os,pathlib,signal,subprocess,sys,time\n"
                "out=pathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'])\n"
                "def finish(*args):\n"
                "    (out/'result.txt').write_text('checkpoint on cancellation')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM,finish)\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
                "(out/'child.pid').write_text(str(child.pid))\n"
                "(out/'ready').write_text('ready')\n"
                "time.sleep(60)\n"
            )
            record = {
                "job_id": "test-signal",
                "owner": "test-owner",
                "deadline": time.time() + 30,
                "config": {
                    "command": [sys.executable, "train.py"],
                    "timeout_seconds": 30,
                    "finalize_seconds": 10,
                    "required_artifacts": ["result.txt"],
                },
            }
            (root / "run.json").write_text(json.dumps(record))
            (root / "owner.json").write_text(
                json.dumps({"job_id": record["job_id"], "owner": record["owner"]})
            )
            process = subprocess.Popen(
                [sys.executable, "-B", runtime.__file__, str(root / "run.json")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                until = time.monotonic() + 10
                while (
                    not (root / "outputs/ready").exists()
                    and process.poll() is None
                    and time.monotonic() < until
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    (root / "outputs/ready").exists(), "workload must start before cancellation"
                )
                supervisor = json.loads(Path("/tmp/podvigil-supervisor.json").read_text())
                self.assertEqual(
                    supervisor,
                    {"job_id": record["job_id"], "owner": record["owner"], "pid": process.pid},
                )
                os.kill(supervisor["pid"], signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 2, stderr)
                manifest = json.loads((root / "manifest.json").read_text())
                self.assertEqual(manifest["training_outcome"], "cancelled")
                data = (root / "outputs/result.txt").read_bytes()
                self.assertEqual(data, b"checkpoint on cancellation")
                self.assertEqual(
                    manifest["files"]["outputs/result.txt"],
                    {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
                )
                child = (root / "outputs/child.pid").read_text()
                self.assertFalse(Path("/proc", child).exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
