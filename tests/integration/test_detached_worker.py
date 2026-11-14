"""Real independent process ownership with an in-memory Modal provider; no cloud calls."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

from podvigil.records import read, save
from podvigil.watchdog_spawn import Monitor, spawn
from tests.support.modal import job

ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def stop_controller(process):
    if process.poll() is None:
        if os.name == "nt":
            # A venv's python.exe may be only a redirector. Kill its complete tree so this
            # test actually removes the controller, not just the launcher process.
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode and process.poll() is None:
                raise RuntimeError("Test controller tree could not be terminated.")
        else:
            process.kill()
    process.wait(timeout=10)


class DetachedWorker(unittest.TestCase):
    def test_worker_finishes_fake_job_after_controller_is_killed(self):
        with tempfile.TemporaryDirectory(prefix="modal-independent-") as temp:
            folder = Path(temp)
            record = job(folder)
            record.update(credential_salt="test", credential_proof={"modal": "dummy"})
            save(folder / "modal-job.json", record)
            save(folder / "job.json", {"provider": "modal"})
            worker_code = (
                f"import sys;sys.path.insert(0,{str(ROOT)!r});import tests;"
                "from pathlib import Path;import time;"
                "from podvigil.modal_backend import runner;"
                "from tests.support.modal import FakeModal;"
                f"folder=Path({str(folder)!r});cloud=FakeModal();"
                "runner.credential_proof=lambda salt:{'modal':'dummy'};"
                "runner.Provider=lambda config:cloud\n"
                "def hook(name):\n"
                " if name=='poll':\n"
                "  end=time.monotonic()+30\n"
                "  while not (folder/'finish').exists() and time.monotonic()<end: time.sleep(.05)\n"
                "cloud.hook=hook\n"
                "runner.worker(folder)\n"
                "(folder/'worker-exited').touch()\n"
            )
            controller_code = (
                f"import sys;sys.path.insert(0,{str(ROOT / 'src')!r});"
                "import time;from pathlib import Path;"
                "from podvigil.watchdog_spawn import spawn;from podvigil.records import save;"
                f"folder=Path({str(folder)!r});"
                f"monitor=spawn(folder,[sys.executable,'-B','-c',{worker_code!r}],"
                "lambda salt:{'modal':'dummy'},require_same_credentials=True);"
                "save(folder/'release.json',{'released':True});"
                "save(folder/'controller-ready.json',{'pid':monitor.pid});"
                "monitor.close();time.sleep(60)"
            )
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            with (folder / "controller.log").open("wb") as log:
                monitor = None
                controller = subprocess.Popen(
                    [sys.executable, "-B", "-c", controller_code], stdout=log, stderr=log, **options
                )
                try:
                    self.assertTrue(
                        wait_for(lambda: (folder / "controller-ready.json").exists()),
                        (folder / "controller.log").read_text(errors="replace"),
                    )
                    proof = read(folder / "watchdog-isolation.json")
                    self.assertFalse(proof["in_job"] if os.name == "nt" else False)
                    if os.name == "nt":
                        monitor = Monitor(read(folder / "controller-ready.json")["pid"])
                    stop_controller(controller)
                    (folder / "finish").touch()
                    self.assertTrue(wait_for(lambda: (folder / "worker-exited").exists()))
                    finished = read(folder / "modal-job.json")
                    self.assertEqual(finished["outcome"], "success")
                    self.assertEqual(finished["volume"]["state"], "deleted")
                    self.assertEqual(finished["sandbox"]["state"], "stopped")
                    self.assertTrue(
                        (Path(finished["backup"]["path"]) / "outputs/result.txt").is_file()
                    )
                finally:
                    stop_controller(controller)
                    (folder / "finish").touch()
                    wait_for(lambda: (folder / "worker-exited").exists(), seconds=5)
                    if monitor is not None:
                        try:
                            # A final marker precedes interpreter shutdown. Windows still denies
                            # deletion of watchdog.log until its owning process has really exited.
                            self.assertTrue(
                                wait_for(lambda: monitor.poll() is not None, seconds=10)
                            )
                        finally:
                            monitor.close()

    def test_mismatched_credentials_never_arm_worker(self):
        with tempfile.TemporaryDirectory(prefix="modal-identity-") as temp:
            folder = Path(temp)
            save(folder / "job.json", {"provider": "modal"})
            command = (
                f"import sys;sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from pathlib import Path;from podvigil.watchdog_spawn import arm;"
                f"p=Path({str(folder)!r});arm(p,lambda salt:{{'modal':'different'}});"
                "(p/'unsafe-release').touch()"
            )
            with self.assertRaisesRegex(RuntimeError, "different credentials"):
                spawn(
                    folder,
                    [sys.executable, "-B", "-c", command],
                    lambda salt: {"modal": "dummy"},
                    require_same_credentials=True,
                )
            self.assertFalse((folder / "unsafe-release").exists())
            for path in folder.glob("watchdog-arm-*.json"):
                pid = (read(path, {}) or {}).get("pid")
                if pid:
                    try:
                        monitor = Monitor(pid)
                    except OSError:
                        continue
                    try:
                        exited = wait_for(
                            lambda: monitor.poll() is not None, seconds=10 if os.name == "nt" else 2
                        )
                        if os.name == "nt":
                            self.assertTrue(exited)
                    finally:
                        monitor.close()
