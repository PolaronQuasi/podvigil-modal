"""Offline code identity for source checkouts and stamped distributions."""

from pathlib import Path
import hashlib
import json
import re
import subprocess

from . import __version__


def identity(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    package = root / "src" / "podvigil"
    if not package.is_dir():
        package = Path(__file__).parent
    result = {"version": __version__, "commit": None, "dirty": None}
    stamp = package / "_build_info.json"
    if stamp.is_file():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            if re.fullmatch(r"[0-9a-f]{40,64}", data.get("commit") or ""):
                result.update(commit=data["commit"], dirty=data.get("dirty"))
        except (OSError, ValueError, TypeError):
            pass
    if (root / ".git").exists():
        try:
            command = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            commit = command.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                changed = subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=True,
                )
                result.update(commit=commit, dirty=bool(changed.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    result["source_sha256"] = digest.hexdigest()
    return result


def version_text():
    result = identity()
    return (
        "podvigil-modal "
        + result["version"]
        + " (commit "
        + (result["commit"] or "unknown")
        + (", modified source" if result["dirty"] else "")
        + ")"
    )
