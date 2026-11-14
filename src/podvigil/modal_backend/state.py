"""Modal-only ledgers, shared by the controller and read-only observers."""

from pathlib import Path
from datetime import datetime
import re
import time
import os
import errno
import math
import sys

from .. import settings
from ..records import read, save
from .errors import UserError
from .events import event
from .pricing import spend_estimate


def jobs_dir():
    return settings.state_dir() / "modal" / "jobs"


def load(folder):
    record = read(Path(folder) / "modal-job.json")
    if not isinstance(record, dict) or record.get("provider") != "modal":
        raise UserError("Not a Modal job ledger; use the commands for its provider.")
    return record


def list_jobs():
    rows = []
    for path in sorted(jobs_dir().glob("*/modal-job.json")):
        try:
            job = load(path.parent)
            rows.append(
                {
                    "job_id": job["job_id"],
                    "ledger": str(path.parent),
                    "created_utc": job.get("created_utc"),
                    "phase": job["phase"],
                    "outcome": job.get("outcome"),
                    "estimated_spend_usd": spend_estimate(job)["estimated_spend_usd"],
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            rows.append(
                {
                    "job_id": path.parent.name,
                    "ledger": str(path.parent),
                    "error": "Unreadable Modal ledger; inspect locally.",
                }
            )
    return rows


def skipped_creation_time(folder):
    """Use the immutable launch sentinel, never the active ledger's modification time."""
    sentinel = folder / "job.json"
    try:
        record = read(sentinel)
        stamp = datetime.fromisoformat(record["created_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.timestamp()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    try:
        value = sentinel.stat().st_mtime
        return value if math.isfinite(value) else None
    except OSError:
        return None


def find_job(name, *, for_mutation=False, warn=lambda line: print(line, file=sys.stderr)):
    path = Path(name)
    if (path / "modal-job.json").is_file() or path.is_dir():
        try:
            load(path)
        except UserError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise UserError(f"Cannot read Modal ledger {path}: {type(exc).__name__}.") from None
        return path.resolve()
    if name != "latest" and not re.fullmatch(r"[0-9a-f]{4,32}", name):
        raise UserError("No Modal ledger at that path.")
    folders = list(jobs_dir().glob("*/modal-job.json"))
    if name == "latest" and for_mutation:
        # Launch writes job.json first. An interrupted launch without its ledger also counts.
        folders = list(
            set(folders) | {p.parent / "modal-job.json" for p in jobs_dir().glob("*/job.json")}
        )

    def created(path, record):
        try:
            stamp = datetime.fromisoformat(record["created_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            value = stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            # Early Modal ledgers have an immutable job.json sentinel, but no timestamp.
            # Never use modal-job.json's mtime: the worker rewrites it while running.
            sentinel = path.parent / "job.json"
            value = sentinel.stat().st_mtime if sentinel.is_file() else 0
        return value, path.parent.name

    readable, skipped = [], []
    for ledger in folders:
        try:
            record = load(ledger.parent)
            readable.append((created(ledger, record), ledger))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            skipped.append(ledger.parent)
            warn(f"Skipping unreadable Modal ledger {ledger.parent} ({type(exc).__name__}).")
    if name == "latest" and for_mutation and readable:
        chosen_time = max(key for key, _ in readable)[0]
        for folder in sorted(skipped):
            age = skipped_creation_time(folder)
            if age is None or age > chosen_time:
                raise UserError(
                    f"Cannot select latest: skipped ledger {folder} may be newer. "
                    "Pass a ledger path or an explicit job ID for cancel or recover."
                )
    folders = [ledger for _, ledger in sorted(readable, key=lambda item: item[0], reverse=True)]
    matches = (
        folders[:1] if name == "latest" else [p for p in folders if p.parent.name.startswith(name)]
    )
    if len(matches) != 1:
        raise UserError("The Modal job name is missing or ambiguous.")
    return matches[0].parent.resolve()


def update(folder, job, **changes):
    previous = job.get("phase")
    job.update(changes)
    job["heartbeat"] = time.time()
    job["progress_at"] = job["heartbeat"]
    changed = previous != job.get("phase") or not job.get("phase_history")
    if changed:
        job.setdefault("phase_history", []).append(
            {"phase": job["phase"], "started_at": job["heartbeat"]}
        )
    save(Path(folder) / "modal-job.json", job)
    if changed:
        event(folder, "phase", job["phase"])


def worker_lock_held(folder):
    """Probe an existing lock read-only. Never create, truncate or write an observer record."""
    try:
        stream = (Path(folder) / "modal-worker.lock").open("rb")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    with stream:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBRLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        except OSError as exc:
            return True if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) else None


def best_effort_update(folder, job, **changes):
    """Cleanup must remain possible when observability writes fail."""
    try:
        update(folder, job, **changes)
        return True
    except Exception:
        return False


def status(folder, now=None):
    job = load(folder)
    now = time.time() if now is None else now
    history = job.get("phase_history", [])
    held = worker_lock_held(folder)
    progress_recent = 0 <= now - job.get("progress_at", job.get("heartbeat", 0)) < 120
    call = job.get("blocking_call")
    call_active = (
        not job.get("outcome")
        and isinstance(call, dict)
        and call.get("name") in ("build_image", "create_volume", "upload", "create_sandbox")
        and all(
            type(call.get(key)) in (int, float) and math.isfinite(call[key])
            for key in ("started_at", "until")
        )
        and call["started_at"] <= now < call["until"] <= job.get("deadline", 0)
    )
    return {
        "provider": "modal",
        "runner": job.get("runner", {"version": None, "commit": None, "dirty": None}),
        "ledger": str(folder),
        "job_id": job["job_id"],
        "phase": job["phase"],
        "phase_history": history,
        "phase_seconds": max(0, now - history[-1]["started_at"]) if history else None,
        "deadline_seconds_left": max(0, job["deadline"] - now) if "deadline" in job else None,
        "startup_seconds_left": max(0, job["startup_deadline"] - now)
        if "startup_deadline" in job
        else None,
        "worker_finishing": bool(job.get("outcome")) and held is not False,
        "recovery_ready": held is False,
        "ended": bool(job.get("outcome")),
        "outcome": job.get("outcome"),
        "job_outcome": job.get("outcome"),
        "loop_heard": progress_recent or call_active,
        "loop_progress_recent": progress_recent,
        "blocking_call": call,
        "blocking_call_active": call_active,
        "startup_waiting": not job.get("outcome")
        and job["phase"]
        in (
            "creating compute",
            "waiting for container",
            "building image",
            "preparing image and app",
        )
        and now < job.get("startup_deadline", job.get("deadline", 0)),
        "deadline_overdue": not bool(job.get("outcome"))
        and job.get("deadline") is not None
        and now >= job["deadline"],
        "last_observed": job.get("last_observed"),
        "observation_error": job.get("observation_error"),
        "runtime_start_error": job.get("runtime_start_error"),
        "log_mirror_error": job.get("log_mirror_error"),
        "sandbox_exit_code": job.get("sandbox_exit_code"),
        "watchdog_heard": 0
        <= now
        - max(
            job.get("heartbeat", 0),
            (read(Path(folder) / "modal-heartbeat.json", {}) or {}).get("at", 0),
        )
        < 120,
        "sandbox": job["sandbox"],
        "volume": job["volume"],
        "deadline": job.get("deadline"),
        "training_outcome": job.get("training_outcome"),
        "supervisor_error": job.get("supervisor_error"),
        "cost_estimate": spend_estimate(job),
        "actual_gpu": job.get("actual_gpu"),
        "gpu_metrics": job.get("gpu_metrics"),
        "backup": job.get("backup"),
        "error": job.get("error"),
        "cleanup_error": job.get("cleanup_error"),
        "stop_signal_error": job.get("stop_signal_error"),
        "failed_phase": job.get("failed_phase"),
        "billing_note": "Stopped compute is distinct from retained storage; deleted Volume data may remain billable for up to four days.",
    }


def remaining_outcome(job):
    if job["sandbox"]["state"] == "unknown" or job["volume"]["state"] == "unknown":
        return "ambiguous"
    if job["sandbox"]["state"] == "exists":
        return "cleanup_unverified"
    if job["volume"]["state"] == "exists":
        return "storage_retained"
    return "error"


def wait(
    folder, timeout=None, *, progress=lambda line: None, clock=time.monotonic, sleep=time.sleep
):
    until = None if timeout is None else clock() + timeout * 60
    last = None
    while True:
        result = status(folder)
        if result["phase"] != last:
            last = result["phase"]
            progress("Modal job: " + last)
        if result["ended"]:
            return result, result["outcome"]
        if result["watchdog_heard"] and not result["loop_heard"] and not result["startup_waiting"]:
            progress(
                "The worker process is alive but its control loop has made no progress for 120 seconds. The job may still run; inspect the ledger and provider deadline."
            )
            return result, remaining_outcome(load(folder))
        if not result["watchdog_heard"]:
            result = status(folder)
            if result["ended"]:
                return result, result["outcome"]
            progress(
                "Modal watchdog is no longer heard; the job may still run. Inspect or recover this ledger."
            )
            return result, remaining_outcome(load(folder))
        if until is not None and clock() >= until:
            return result, "wait_timeout"
        sleep(1 if until is None else min(1, max(0, until - clock())))
