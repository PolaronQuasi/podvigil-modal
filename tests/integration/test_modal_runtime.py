"""Run the actual Sandbox entrypoint on local Linux without Modal or network."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.modal_backend import runtime


@unittest.skipUnless(sys.platform.startswith("linux"), "Sandbox runtime requires Linux")
class ModalRuntime(unittest.TestCase):
    def invoke(self, code, seconds=15, *, periodic_failure=False, config=None, fake_gpu=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        (folder / "inputs").mkdir()
        (folder / "inputs/train.py").write_text(code)
        record = {
            "job_id": "test",
            "owner": "owner",
            "deadline": time.time() + seconds,
            "config": {
                "command": [sys.executable, "train.py"],
                "finalize_seconds": 5,
                "required_artifacts": ["result.txt"],
            },
        }
        record["config"].update(config or {})
        (folder / "run.json").write_text(json.dumps(record))
        (folder / "owner.json").write_text(json.dumps({"job_id": "test", "owner": "owner"}))
        entrypoint = runtime.__file__
        if periodic_failure:
            entrypoint = str(folder / "supervisor-test.py")
            Path(entrypoint).write_text(
                "import importlib.util,pathlib,subprocess,sys\n"
                + "spec=importlib.util.spec_from_file_location('runtime', "
                + repr(runtime.__file__)
                + ")\n"
                + "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                + "m.CHECKPOINT_INTERVAL=0.01\noriginal=m.sync_volume\n"
                + "def sync(root, deadline, *, periodic=False):\n"
                + "    if periodic: raise subprocess.TimeoutExpired('sync',20)\n"
                + "    return original(root,deadline)\n"
                + "m.sync_volume=sync\nraise SystemExit(m.run(pathlib.Path(sys.argv[1])))\n"
            )
        environment = dict(os.environ, MODAL_TOKEN_SECRET="must-not-reach-workload")
        if fake_gpu:
            binary = folder / "bin"
            binary.mkdir()
            executable = binary / "nvidia-smi"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' 'Fake T4, 40, 1024'\n")
            executable.chmod(0o700)
            environment["PATH"] = str(binary) + os.pathsep + environment.get("PATH", "")
        result = subprocess.run(
            [sys.executable, "-B", entrypoint, str(folder / "run.json")],
            capture_output=True,
            timeout=25,
            env=environment,
        )
        return folder, result, json.loads((folder / "manifest.json").read_text())

    def test_success_manifest_and_credential_boundary(self):
        folder, result, record = self.invoke(
            "import os,pathlib\nassert 'MODAL_TOKEN_SECRET' not in os.environ\npathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'],'result.txt').write_text('ok')\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["training_outcome"], "success")
        self.assertEqual(record["files"]["outputs/result.txt"]["size"], 2)

    def test_rented_resources_and_user_environment_reach_only_workload(self):
        folder, result, record = self.invoke(
            "import os,pathlib\n"
            "assert 'MODAL_TOKEN_SECRET' not in os.environ\n"
            "assert os.environ['OMP_NUM_THREADS']=='2'\n"
            "assert os.environ['PODVIGIL_CPU']=='2'\n"
            "assert os.environ['PODVIGIL_MEMORY_MIB']=='4096'\n"
            "assert os.environ['PODVIGIL_GPU']=='Fake T4'\n"
            "assert 0 < float(os.environ['PODVIGIL_SECONDS_AVAILABLE']) <= 10\n"
            "pathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'],'result.txt').write_text('ok')\n",
            config={
                "cpu": 2,
                "memory_mib": 4096,
                "gpu": "T4",
                "workload_env": {"OMP_NUM_THREADS": "2"},
            },
            fake_gpu=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["gpu_metrics"]["gpu_names"], ["Fake T4"])
        self.assertIn("outputs/.podvigil/gpu-samples.jsonl", record["files"])
        self.assertGreater(record["timing"]["supervisor_seconds"], 0)

    def test_failure_and_missing_artifact(self):
        for code, expected in (
            ("raise SystemExit(3)", "training_failed"),
            ("pass", "missing_artifacts"),
        ):
            _, result, record = self.invoke(code)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(record["training_outcome"], expected)

    def test_slow_periodic_sync_preserves_successful_work_and_diagnostic(self):
        _, result, record = self.invoke(
            "import time,os,pathlib\ntime.sleep(0.4)\npathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'],'result.txt').write_text('ok')",
            periodic_failure=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["training_outcome"], "success")
        self.assertGreater(record["diagnostics"]["periodic_sync_failures"], 0)

    def test_links_are_preserved_as_metadata_without_reading_their_targets(self):
        folder, result, record = self.invoke(
            "import os,pathlib\nout=pathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'])\n(out/'result.txt').write_text('ok')\n(out/'latest-run').symlink_to('result.txt')\n(out/'external').symlink_to('/etc/passwd')\n(out/'directory').symlink_to('/etc', target_is_directory=True)"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        links = json.loads((folder / "output-links.json").read_text())["links"]
        self.assertEqual(links["outputs/latest-run"], "result.txt")
        self.assertEqual(links["outputs/external"], "/etc/passwd")
        self.assertIn("output-links.json", record["files"])
        self.assertNotIn("outputs/external", record["files"])
        self.assertFalse((folder / "outputs/directory").is_symlink())

    def test_link_metadata_conflict_refuses_overwriting_workload_data(self):
        # Exercise the standalone serializer; no process or cloud is needed.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "link").symlink_to("/etc/passwd")
            (folder / "output-links.json").write_text("keep me")
            with self.assertRaises(FileExistsError):
                runtime.preserve_links(folder)
            self.assertTrue((folder / "link").is_symlink())
            self.assertEqual((folder / "output-links.json").read_text(), "keep me")

    def test_timeout_checkpoints_and_session_detached_child_cleanup(self):
        code = """import os,pathlib,subprocess,sys,time
out=pathlib.Path(os.environ['PODVIGIL_OUTPUT_DIR'])
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)
(out/'child.pid').write_text(str(child.pid))
(out/'result.txt').write_text('checkpoint')
time.sleep(60)
"""
        folder, result, record = self.invoke(code, seconds=6)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(record["training_outcome"], "time_limit")
        pid = int((folder / "outputs/child.pid").read_text())
        self.assertFalse(Path("/proc", str(pid)).exists())
