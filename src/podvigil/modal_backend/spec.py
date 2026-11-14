"""Offline validation and explicit-file input staging for Modal jobs."""

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import hashlib
import json
import math
import os
import re
import shutil

from .errors import UserError
from .pricing import price_check


def portable_path(value):
    """One relative, portable file path; never an alias or traversal."""
    if not isinstance(value, str) or not value or len(value) > 240:
        raise UserError("Expected a portable relative file path.")
    parts = value.split("/")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.= -]+", part)
        or part in (".", "..")
        or part.endswith((".", " "))
        or part.split(".")[0].upper() in reserved
        for part in parts
    ):
        raise UserError("Unsafe or nonportable file path.")
    return PurePosixPath(value).as_posix()


def number(value, label, low, high, *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise UserError(f"{label} must be between {low} and {high}.")
    if integer and type(value) is not int:
        raise UserError(f"{label} must be an integer.")
    return value


def read_spec(path):
    path = Path(path)
    try:
        path = path.resolve()
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(
            f"Cannot read job file {path}: {exc.strerror or type(exc).__name__}."
        ) from None
    except UnicodeError as exc:
        raise UserError(f"Job file {path} is not valid UTF-8 (byte {exc.start}).") from None
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise UserError(
            f"Invalid JSON in job file {path}, line {exc.lineno}, column {exc.colno}: {exc.msg}."
        ) from None
    allowed = {
        "provider",
        "app",
        "environment",
        "image",
        "gpu",
        "cpu",
        "memory_mib",
        "timeout_seconds",
        "startup_timeout_seconds",
        "finalize_seconds",
        "command",
        "inputs",
        "required_artifacts",
        "estimated_hourly_usd",
        "max_compute_cost_usd",
        "rate_checked_utc",
        "block_network",
        "max_backup_bytes",
        "max_backup_files",
        "workload_env",
    }
    if not isinstance(raw, dict) or set(raw) - allowed or raw.get("provider") != "modal":
        raise UserError("Expected a Modal job with known fields and provider=modal.")
    spec = dict(raw)
    environment = spec.get("workload_env", {})
    if not isinstance(environment, dict) or len(environment) > 128:
        raise UserError("workload_env must map at most 128 names to non-secret strings.")
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
            or name.upper().startswith(("PODVIGIL_", "MODAL_"))
            or not isinstance(value, str)
            or "\0" in value
            or len(value.encode("utf-8")) > 8192
        ):
            raise UserError(
                "Invalid workload_env entry; PODVIGIL_/MODAL_ names are reserved, values must be non-secret strings without NUL (at most 8192 bytes)."
            )
    if sum(len(k.encode()) + len(v.encode()) for k, v in environment.items()) > 65536:
        raise UserError("workload_env exceeds its 65536-byte limit.")
    for key in ("app", "environment"):
        if not isinstance(spec.get(key), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", spec[key]
        ):
            raise UserError(f"{key} must be an explicit Modal name.")
    image = spec.get("image")
    if not isinstance(image, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/:@-]{1,240}", image
    ):
        raise UserError("image must be a trusted public Linux image reference.")
    gpu = spec.get("gpu")
    if gpu is not None and (
        not isinstance(gpu, str)
        or not re.fullmatch(
            r"(?:T4|L4|A10|L40S|A100-40GB|A100-80GB|H100|H200|B200|B300|RTX-PRO-6000)(?::[1-8])?",
            gpu,
        )
    ):
        raise UserError("gpu must be a supported explicit GPU reservation, or null for CPU.")
    defaults = {
        "cpu": (1, 0.125, 64, False),
        "memory_mib": (4096, 128, 262144, True),
        "timeout_seconds": (None, 30, 86400, True),
        "finalize_seconds": (60, 5, 600, True),
        "estimated_hourly_usd": (None, 0.000001, 10000, False),
        "max_compute_cost_usd": (None, 0.000001, 100000, False),
        "max_backup_bytes": (10 * 1024**3, 1, 1024**4, True),
        "max_backup_files": (10000, 1, 100000, True),
    }
    for key, (default, low, high, integer) in defaults.items():
        spec[key] = number(spec.get(key, default), key, low, high, integer=integer)
    if "startup_timeout_seconds" in spec:
        number(spec["startup_timeout_seconds"], "startup_timeout_seconds", 1, 86400, integer=True)
    if spec["finalize_seconds"] >= spec["timeout_seconds"] - 5:
        raise UserError("Reserve at least five seconds for the workload before finalization.")
    if spec["estimated_hourly_usd"] * spec["timeout_seconds"] / 3600 > spec["max_compute_cost_usd"]:
        raise UserError("The estimated compute cost exceeds max_compute_cost_usd.")
    try:
        checked = datetime.fromisoformat(spec["rate_checked_utc"].replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise UserError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UserError("rate_checked_utc must be an ISO 8601 time with a timezone.") from None
    command = spec.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(s, str) or not s or "\0" in s for s in command)
    ):
        raise UserError("command must be a nonempty argument array, without secrets.")
    spec.setdefault("block_network", False)
    if type(spec["block_network"]) is not bool:
        raise UserError("block_network must be a boolean.")
    required = spec.setdefault("required_artifacts", [])
    if not isinstance(required, list):
        raise UserError("required_artifacts must be a list of output-relative paths.")
    spec["required_artifacts"] = [portable_path(s) for s in required]
    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict):
        raise UserError("inputs maps destination paths to individual local files.")
    sources, aliases = {}, set()
    for destination, source in inputs.items():
        destination = portable_path(destination)
        if not isinstance(source, str):
            raise UserError("Input sources must be paths.")
        candidate = path.parent / source
        if any(p.is_symlink() for p in (candidate, *candidate.parents)):
            raise UserError("Input links are not allowed.")
        candidate = candidate.resolve()
        if (
            not candidate.is_file()
            or candidate.name.lower().startswith(".env")
            or candidate.suffix.lower() in (".key", ".pem", ".p12", ".pfx")
            or candidate.name.lower() in ("credentials.env", ".modal.toml")
        ):
            raise UserError("Inputs must be ordinary files, not credential files.")
        if destination.casefold() in aliases:
            raise UserError("Input paths must not collide across filesystems.")
        aliases.add(destination.casefold())
        sources[destination] = candidate
    for a in aliases:
        if any(b.startswith(a + "/") for b in aliases):
            raise UserError("An input file cannot also be a parent directory.")
    spec["inputs"] = sorted(sources)
    if sum(p.stat().st_size for p in sources.values()) >= spec["max_backup_bytes"]:
        raise UserError("Input files alone exhaust max_backup_bytes; increase it before launch.")
    if len(sources) + 3 >= spec["max_backup_files"]:
        raise UserError(
            "Input files and job metadata exhaust max_backup_files; increase it before launch."
        )
    return spec, sources


def check_quote(spec, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    age = now - datetime.fromisoformat(spec["rate_checked_utc"].replace("Z", "+00:00")).timestamp()
    if not 0 <= age <= 86400:
        raise UserError(
            "Refresh the estimated Modal rate: rate_checked_utc must be within the last 24 hours."
        )


def plan(path):
    spec, sources = read_spec(path)
    return {
        "provider": "modal",
        "config": spec,
        "input_bytes": sum(p.stat().st_size for p in sources.values()),
        "startup_allowance_seconds": spec.get("startup_timeout_seconds"),
        "run_allowance_seconds": spec["timeout_seconds"],
        "total_allowance_seconds": spec["timeout_seconds"] + spec.get("startup_timeout_seconds", 0),
        "estimated_compute_usd": spec["estimated_hourly_usd"] * spec["timeout_seconds"] / 3600,
        "pricing": "User-supplied compute estimate; excludes storage, egress and builds. Not a spending cap.",
        "cloud_calls": 0,
        "price_check": price_check(spec),
    }


def stage(sources, folder):
    records = {}
    for name, source in sources.items():
        target = folder / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        h = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        records["inputs/" + name] = {"size": target.stat().st_size, "sha256": h.hexdigest()}
    return records
