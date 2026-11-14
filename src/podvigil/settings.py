"""Local Modal job state. Credentials are managed exclusively by the Modal SDK."""

from pathlib import Path
import os
import sys

STATE_DIR_VARIABLE = "PODVIGIL_MODAL_STATE_DIR"
ENV_PREFIX = "PODVIGIL_MODAL"
ENV_PREFIXES = (ENV_PREFIX,)
_state_dir = None


def configure(*, state_dir=None):
    global _state_dir
    _state_dir = None if state_dir is None else Path(state_dir).expanduser().resolve()


def default_state_dir():
    name = "podvigil-modal"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / name
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support" / name
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local/state") / name


def state_dir():
    if _state_dir is not None:
        return _state_dir
    named = os.environ.get(STATE_DIR_VARIABLE)
    return Path(named).expanduser().resolve() if named else default_state_dir()
