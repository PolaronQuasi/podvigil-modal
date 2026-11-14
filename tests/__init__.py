"""Offline tests: isolate all state and replace Modal credentials with dummy values."""

from pathlib import Path
import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
_SESSION = tempfile.mkdtemp(prefix="podvigil-modal-tests-")
atexit.register(shutil.rmtree, _SESSION, True)
os.environ["PODVIGIL_MODAL_STATE_DIR"] = os.path.join(_SESSION, "state")
os.environ["MODAL_TOKEN_ID"] = "ak-test-only"
os.environ["MODAL_TOKEN_SECRET"] = "as-test-only"
os.environ["MODAL_CONFIG_PATH"] = os.path.join(_SESSION, "modal.toml")
os.environ["MODAL_PROFILE"] = "test"
