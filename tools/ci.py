"""Offline validation and package checks for the Modal-only Gitea project."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(
    rb"(?:ak|as)-[A-Za-z0-9]{20,}"
    rb"|(?:rpa_|rps_|rp_)[A-Za-z0-9]{24,}"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    rb"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|(?:sk-|hf_)[A-Za-z0-9_-]{30,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def tracked():
    result = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
    )
    return sorted(
        {name for name in result.decode().split("\0") if name and (ROOT / name).is_file()}
    )


def forbidden(name):
    p = PurePosixPath(name)
    return (
        any(
            part in {".git", ".aws", ".ssh", "output", "downloads", "__pycache__", "node_modules"}
            or part.startswith(".venv")
            for part in p.parts
        )
        or p.name.startswith(".env")
        or p.name in {".modal.toml", ".runner", "credentials.env"}
        or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def audit():
    for name in tracked():
        if forbidden(name) or SECRET.search((ROOT / name).read_bytes()):
            raise RuntimeError("Private file or possible credential: " + name)
    subprocess.run(["git", "diff", "--check", "HEAD"], cwd=ROOT, check=True)
    print("Credential and whitespace audit passed.")


def source_hashes():
    paths = []
    for name in ("src", "tests", "tools", "docs", "examples", ".gitea", ".github"):
        paths.extend(
            p
            for p in (ROOT / name).rglob("*")
            if p.is_file()
            and not forbidden(p.relative_to(ROOT).as_posix())
            and not any(part.endswith(".egg-info") for part in p.parts)
            and p.suffix not in {".pyc", ".pyo"}
        )
    paths.extend(
        p
        for p in ROOT.iterdir()
        if p.is_file()
        and (p.suffix in {".md", ".toml", ".txt", ".py"} or p.name in {"LICENSE", "NOTICE"})
    )
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


def test(lane):
    sys.path.insert(0, str(ROOT))
    before = source_hashes()
    start = ROOT / "tests" if lane == "all" else ROOT / "tests" / lane
    suite = unittest.defaultTestLoader.discover(str(start), top_level_dir=str(ROOT))
    output = ROOT / "output/ci"
    output.mkdir(parents=True, exist_ok=True)
    with (output / (lane + "-tests.log")).open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    after = source_hashes()
    report = {
        "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.skipped) - len(result.failures) - len(result.errors),
        "skipped": [{"test": t.id(), "reason": reason} for t, reason in result.skipped],
        "failures": [{"test": t.id(), "detail": detail} for t, detail in result.failures],
        "errors": [{"test": t.id(), "detail": detail} for t, detail in result.errors],
        "source_sha256": after,
        "source_changed": before != after,
        "successful": result.wasSuccessful() and before == after and result.testsRun > 0,
        "cloud_calls": False,
    }
    (output / (lane + "-tests.json")).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: report[k] for k in ("tests_run", "passed", "source_changed", "successful")}
            | {"skipped": len(result.skipped)}
        )
    )
    if not report["successful"]:
        for t, detail in result.failures + result.errors:
            print(t.id() + "\n" + detail)
        raise RuntimeError("Offline suite failed; see output/ci.")


def members(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [
                (name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")
            ]
    with tarfile.open(path, "r:gz") as archive:
        return [
            (item.name, archive.extractfile(item).read())
            for item in archive.getmembers()
            if item.isfile()
        ]


def smoke():
    sys.path.insert(0, str(ROOT / "src"))
    from podvigil import __version__
    from podvigil.identity import identity

    expected_identity = identity(ROOT)

    wheel = ROOT / "dist" / ("podvigil_modal-" + __version__ + "-py3-none-any.whl")
    sdist = ROOT / "dist" / ("podvigil_modal-" + __version__ + ".tar.gz")
    for path in (wheel, sdist):
        stamps = []
        for name, data in members(path):
            normalized = "/".join(PurePosixPath(name).parts[1:]) if path == sdist else name
            if forbidden(normalized) or ".." in PurePosixPath(name).parts or SECRET.search(data):
                raise RuntimeError("Private or unsafe package member: " + name)
            if any(
                p in normalized
                for p in ("podvigil/provider/", "podvigil/controller/", "podvigil/podrt/")
            ):
                raise RuntimeError("Unexpected provider package: " + name)
            if normalized.endswith("podvigil/_build_info.json"):
                stamps.append(json.loads(data))
            if name.endswith(("METADATA", "PKG-INFO")):
                requires = [
                    line for line in data.decode().splitlines() if line.startswith("Requires-Dist:")
                ]
                if requires != ["Requires-Dist: modal==1.6.4"]:
                    raise RuntimeError("Unexpected runtime dependency metadata")
        if len(stamps) != 1 or any(
            stamps[0].get(key) != expected_identity[key]
            for key in ("version", "commit", "source_sha256")
        ):
            raise RuntimeError("Package build identity does not match the source being checked")
    with tempfile.TemporaryDirectory(prefix="podvigil-modal-wheel-") as temp:
        root = Path(temp)
        subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True)
        bin_dir = root / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        command = bin_dir / ("podvigil-modal.exe" if os.name == "nt" else "podvigil-modal")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        env = dict(os.environ)
        for name in list(env):
            if name.startswith("MODAL_") or name == "PYTHONPATH":
                env.pop(name)
        env["MODAL_CONFIG_PATH"] = str(root / "absent.toml")
        env["PODVIGIL_MODAL_STATE_DIR"] = str(root / "unused-state")
        for prefix in ([str(command)], [str(python), "-I", "-B", "-m", "podvigil"]):
            result = subprocess.run(
                [*prefix, "plan", str(ROOT / "examples/modal-smoke/modal-job.json")],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(result.stdout)["cloud_calls"] == 0
            version_result = subprocess.run(
                [*prefix, "--version"],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            assert __version__ in version_result.stdout
            assert (expected_identity["commit"] or "unknown") in version_result.stdout
        assert not (root / "unused-state").exists()
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.util; assert importlib.util.find_spec('modal') is None",
            ],
            cwd=root,
            check=True,
        )
    print("Wheel/sdist audit and installed Modal-only CLI smoke passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tests = commands.add_parser("test")
    tests.add_argument("--lane", choices=("all", "unit", "fakecloud", "integration"), default="all")
    commands.add_parser("audit")
    commands.add_parser("smoke")
    args = parser.parse_args()
    if args.command == "test":
        test(args.lane)
    else:
        {"audit": audit, "smoke": smoke}[args.command]()


if __name__ == "__main__":
    main()
