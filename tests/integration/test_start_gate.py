"""Actual Linux entrypoint refuses workload execution without a committed start gate."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.modal_backend import runtime


@unittest.skipUnless(sys.platform.startswith("linux"), "Sandbox runtime requires Linux")
class StartGate(unittest.TestCase):
    def test_expired_start_and_failed_commit_never_execute_workload(self):
        for expired in (False, True):
            with self.subTest(expired=expired), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "workload-was-run"
                config = {
                    "job_id": "test",
                    "owner": "owner",
                    "start_protocol": 1,
                    "startup_deadline": time.time() + (-1 if expired else 20),
                    "deadline": time.time() + 50,
                    "config": {
                        "timeout_seconds": 30,
                        "finalize_seconds": 5,
                        "required_artifacts": [],
                        "command": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()",
                        ],
                    },
                }
                path = root / "run.json"
                path.write_text(json.dumps(config))
                launcher = (
                    "import importlib.util,pathlib;"
                    "s=importlib.util.spec_from_file_location('runtime',"
                    + repr(runtime.__file__)
                    + ");"
                    "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                    "m.sync_volume=lambda *a,**k: (_ for _ in ()).throw(OSError('fake failed commit'));"
                    "raise SystemExit(m.main(pathlib.Path(" + repr(str(path)) + ")))"
                )
                result = subprocess.run(
                    [sys.executable, "-B", "-c", launcher], capture_output=True, timeout=10
                )
                self.assertEqual(result.returncode, 75 if expired else 1, result.stderr)
                self.assertFalse(marker.exists())
                if expired:
                    self.assertEqual({p.name for p in root.iterdir()}, {"run.json"})
                else:
                    self.assertTrue((root / "runtime-start.json").exists())
                    self.assertTrue((root / "supervisor-error.json").exists())
