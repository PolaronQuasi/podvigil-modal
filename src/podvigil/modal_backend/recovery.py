"""Stop the receipted Sandbox, audit every Volume file, then delete by receipted ID."""

from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import re
import time
import uuid
import math

from ..records import save
from .spec import portable_path
from .errors import UserError, LifecycleError
from .state import best_effort_update, update
from .logs import from_backup


def operator_absence(job):
    """An explicit operator statement is distinct from observed terminal compute."""
    resource = job["sandbox"]
    assertion = resource.get("operator_assertion", {})
    return (
        resource["state"] == "operator_asserted_absent"
        and isinstance(assertion, dict)
        and assertion.get("source") == "operator"
        and assertion.get("lookup_result") == "not_found"
        and isinstance(assertion.get("statement"), str)
        and all(
            type(assertion.get(k)) in (int, float) and math.isfinite(assertion[k])
            for k in ("at", "eligible_after")
        )
        and assertion["at"] >= assertion["eligible_after"]
    )


def stop_compute(provider, folder, job, *, sleep=time.sleep):
    resource = job["sandbox"]
    if resource["state"] == "operator_asserted_absent":
        if not operator_absence(job):
            raise UserError(
                "Operator absence statement is missing or invalid; storage is retained."
            )
        return  # Never relabel the assertion as stopped compute or a terminal observation.
    if resource["state"] in ("not_created", "stopped"):
        return
    if resource["state"] == "unknown":
        found = provider.resolve_sandbox(job)
        if found is not None:
            resource.update(
                state="exists", id=found, evidence="creation intent and both ownership tags"
            )
            best_effort_update(folder, job)
    if resource["state"] == "unknown" or not resource.get("id"):
        raise LifecycleError("Sandbox creation is unresolved; no Volume deletion is permitted.")
    best_effort_update(folder, job, phase="stopping compute")
    try:
        running = provider.poll(job) is None
    except Exception:
        running = True
    if running:
        try:
            # terminate independently verifies the ownership tags. A failed status read or
            # ledger write must not prevent this attempt; a lost reply still needs read-back.
            provider.terminate(job)
        except Exception:
            pass
    # An accepted termination request is not completion evidence.
    terminal_reads = 0
    for _ in range(30):
        try:
            code = provider.poll(job)
            terminal_reads = terminal_reads + 1 if code is not None else 0
            if terminal_reads == 2:
                job["sandbox"]["state"] = "stopped"
                best_effort_update(folder, job, sandbox_exit_code=code, terminal_observations=2)
                return
        except Exception:
            terminal_reads = 0
        sleep(1)
    raise LifecycleError("Sandbox termination has not been verified.")


def flush_backup_directories(backup, filenames):
    """Persist new directory entries bottom-up before deleting the remote copy (POSIX)."""
    if os.name == "nt":
        return
    directories = {backup, backup.parent}
    for name in filenames:
        parent = (backup / name).parent
        while parent != backup:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def completed_outcome(job):
    """Recover the audited result across a crash after verified storage deletion."""
    if job["sandbox"]["state"] not in ("not_created", "stopped") and not operator_absence(job):
        return None
    if job["volume"]["state"] == "deleted" and job.get("delete_requested"):
        outcome = (job.get("backup") or {}).get("outcome")
        if operator_absence(job) and outcome != "error":
            return None
        if outcome in (
            "success",
            "workload_failed",
            "partial_outputs",
            "cancelled",
            "no_capacity",
        ) or (outcome == "error" and (job.get("unstarted_backup") or operator_absence(job))):
            return outcome
    return None


def read_small(provider, volume_id, name, limit=1024 * 1024):
    chunks, size = [], 0
    for chunk in provider.read_file(volume_id, name):
        size += len(chunk)
        if size > limit:
            raise UserError("Volume metadata exceeds its size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def inventory(provider, job):
    files = provider.files(job["volume"]["id"])
    spec, total = job["config"], 0
    if len(files) > spec["max_backup_files"]:
        raise UserError("Volume exceeds max_backup_files; storage is retained.")
    for name, size in files.items():
        remote_path(name)
        if type(size) is not int or size < 0:
            raise UserError("Volume inventory has an invalid file size.")
        total += size
    if any(
        str(parent) in files
        for name in files
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    ):
        raise UserError("A Volume file is also a parent directory.")
    if total > spec["max_backup_bytes"]:
        raise UserError("Volume exceeds max_backup_bytes; storage is retained.")
    return files


def remote_path(name):
    if (
        not isinstance(name, str)
        or not name
        or "\0" in name
        or name.startswith("/")
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise UserError("Volume inventory contains an unsafe relative path.")
    return name


def backup_paths(files):
    """Preserve normal paths; encode the whole snapshot when a platform would alias/reject one."""
    try:
        aliases = set()
        for name in files:
            portable_path(name)
            if name.casefold() in aliases:
                raise UserError("Case collision")
            aliases.add(name.casefold())
        if any(
            str(p).casefold() in aliases
            for name in files
            for p in PurePosixPath(name).parents
            if str(p) != "."
        ):
            raise UserError("Case-folded parent collision")
        return {name: name for name in files}
    except UserError:
        return {name: "files/" + hashlib.sha256(name.encode("utf-8")).hexdigest() for name in files}


def pending_backup(folder, job, files):
    stamp = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    pending = job.get("pending_backup") or {}
    name = pending.get("name", "")
    if pending.get("inventory") != stamp or not re.fullmatch(r"backup-[0-9a-f]{32}", name):
        name = "backup-" + uuid.uuid4().hex
    backup = Path(folder) / name
    if backup.is_symlink():
        raise UserError(
            "The pending backup is a link; retain storage and inspect the local ledger."
        )
    backup.mkdir(mode=0o700, exist_ok=True)
    update(folder, job, pending_backup={"name": name, "inventory": stamp})
    return backup


def recover_outputs(
    provider,
    folder,
    job,
    *,
    accept_partial=False,
    unstarted=False,
    require_no_start=False,
    sleep=time.sleep,
):
    stop_compute(provider, folder, job, sleep=sleep)
    resource = job["volume"]
    if resource["state"] in ("not_created", "deleted"):
        return completed_outcome(job) or job.get("outcome") or "error"
    if resource["state"] != "exists" or not resource.get("id"):
        raise LifecycleError("Volume creation is unresolved; nothing is deleted by name.")
    volume_id = resource["id"]
    # A previously audited delete whose response was lost can be completed by ID read-back.
    if job.get("delete_requested") and job.get("backup") and not provider.volume_exists(volume_id):
        sleep(1)
        if not provider.volume_exists(volume_id):
            resource["state"] = "deleted"
            update(folder, job)
            return job["backup"]["outcome"]
    update(folder, job, phase="verifying outputs")
    files = inventory(provider, job)
    expected_upload = job.get("upload_manifest", {})
    asserted = operator_absence(job)
    never_started = (
        job.get("start_protocol") == 1
        and job["sandbox"]["state"] == "stopped"
        and bool(job["sandbox"].get("id"))
        and job.get("terminal_observations") == 2
        and "runtime-start.json" not in files
        and {"owner.json", "run.json"} <= set(expected_upload)
        and set(files) == set(expected_upload)
    )
    if require_no_start and not never_started:
        raise UserError(
            "Startup failure lacks proof that the workload never started; storage is retained."
        )
    unstarted = (
        unstarted
        or never_started
        or (
            asserted
            and bool(expected_upload)
            and "owner.json" in files
            and "manifest.json" not in files
            and set(files) == set(expected_upload)
        )
    )
    if unstarted and (
        (job["sandbox"]["state"] != "not_created" and not never_started and not asserted)
        or not expected_upload
        or not set(files) <= set(expected_upload)
    ):
        raise UserError(
            "Unstarted cleanup cannot prove these are only this attempt's staged uploads; storage is retained."
        )
    if "owner.json" in files:
        owner = json.loads(read_small(provider, volume_id, "owner.json"))
        if owner != {"job_id": job["job_id"], "owner": job["owner"]}:
            raise UserError("Volume ownership record does not match its creation receipt.")
    elif not (unstarted and not asserted and resource.get("create_acknowledged")):
        raise UserError("Volume ownership record is missing; storage is retained.")
    supervisor_error = None
    diagnostic_receipt = None
    diagnostic_name = "supervisor-error.json"
    if diagnostic_name in files:
        diagnostic_data = read_small(provider, volume_id, diagnostic_name, 65536)
        diagnostic_receipt = {
            "size": len(diagnostic_data),
            "sha256": hashlib.sha256(diagnostic_data).hexdigest(),
        }
        try:
            supervisor_error = json.loads(diagnostic_data)
            if (
                not isinstance(supervisor_error, dict)
                or set(supervisor_error) != {"error_type"}
                or not isinstance(supervisor_error["error_type"], str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", supervisor_error["error_type"])
            ):
                raise ValueError
        except ValueError:
            if not accept_partial:
                raise UserError(
                    "supervisor-error.json cannot be parsed or validated; storage is retained. "
                    "Use recover --accept-partial to back up this diagnostic as opaque bytes."
                ) from None
            supervisor_error = {
                "unreadable": True,
                "note": "supervisor-error.json could not be parsed or validated; backed up as opaque bytes with --accept-partial.",
            }
    manifest = None
    manifest_receipt = None
    unlisted_diagnostics = set()
    if "manifest.json" in files:
        manifest_data = read_small(provider, volume_id, "manifest.json", 32 * 1024**2)
        manifest_receipt = {
            "size": len(manifest_data),
            "sha256": hashlib.sha256(manifest_data).hexdigest(),
        }
        manifest = json.loads(manifest_data)
        if manifest.get("job_id") != job["job_id"] or manifest.get("owner") != job["owner"]:
            raise UserError("Output manifest belongs to another job.")
        if diagnostic_name in files and diagnostic_name not in manifest.get("files", {}):
            unlisted_diagnostics.add(diagnostic_name)
        if set(manifest.get("files", {})) != set(files) - {"manifest.json"} - unlisted_diagnostics:
            raise UserError("Output manifest does not cover every Volume file.")
    if manifest is None and not accept_partial and not unstarted:
        raise UserError(
            "No complete output manifest; keep storage or explicitly recover --accept-partial."
        )
    backup = pending_backup(folder, job, files)
    paths = backup_paths(files)
    hashes = {}
    last_progress = time.monotonic()

    def pulse():
        nonlocal last_progress
        if time.monotonic() - last_progress >= 5:
            best_effort_update(folder, job)
            last_progress = time.monotonic()

    for name, size in sorted(files.items()):
        target = backup / paths[name]
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise UserError("A local backup path is a link; storage is retained.")
        target.parent.mkdir(parents=True, exist_ok=True)
        h, count = hashlib.sha256(), 0
        with target.open("wb") as stream:
            for chunk in provider.read_file(volume_id, name):
                count += len(chunk)
                if count > size:
                    raise UserError("A Volume file grew during backup.")
                h.update(chunk)
                stream.write(chunk)
                pulse()
            stream.flush()
            os.fsync(stream.fileno())
        actual = {"size": count, "sha256": h.hexdigest()}
        if (
            count != size
            or (name == "manifest.json" and actual != manifest_receipt)
            or (name == diagnostic_name and actual != diagnostic_receipt)
            or (
                manifest is not None
                and name != "manifest.json"
                and name not in unlisted_diagnostics
                and manifest["files"][name] != actual
            )
            or (unstarted and expected_upload[name] != actual)
        ):
            raise UserError("Backup bytes do not match the Volume manifest.")
        # Verify the persisted local bytes too, not only the incoming stream.
        local = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                local.update(chunk)
                pulse()
        if local.hexdigest() != actual["sha256"]:
            raise UserError("Local backup verification failed.")
        hashes[name] = actual
        best_effort_update(folder, job)
    # Independent second reads also cover the manifest and metadata; never trust size alone.
    if inventory(provider, job) != files:
        raise UserError("Volume inventory changed during backup.")
    for name, expected in hashes.items():
        h, count = hashlib.sha256(), 0
        for chunk in provider.read_file(volume_id, name):
            count += len(chunk)
            if count > expected["size"]:
                raise UserError("Volume changed during verification.")
            h.update(chunk)
            pulse()
        if {"size": count, "sha256": h.hexdigest()} != expected:
            raise UserError("Volume changed during verification.")
        best_effort_update(folder, job)
    if inventory(provider, job) != files:
        raise UserError("Volume inventory changed before deletion.")
    training = manifest.get("training_outcome") if manifest else "partial_outputs"
    timing = manifest.get("timing") if manifest else None
    if timing is not None and (
        not isinstance(timing, dict)
        or type(timing.get("supervisor_seconds")) not in (int, float)
        or not math.isfinite(timing["supervisor_seconds"])
        or timing["supervisor_seconds"] < 0
    ):
        raise UserError("Runtime timing receipt is invalid; storage is retained.")
    if (
        manifest
        and training == "success"
        and any("outputs/" + name not in files for name in job["config"]["required_artifacts"])
    ):
        raise UserError("Required output artifacts are missing.")
    outcome = (
        "partial_outputs"
        if manifest is None
        else ("success" if training == "success" else "workload_failed")
    )
    if job.get("cancel_requested") and training in ("success", "cancelled"):
        outcome = "cancelled"
    if unstarted:
        outcome = (
            "cancelled"
            if job.get("cancel_requested")
            else (
                "no_capacity"
                if never_started
                or (job.get("startup_expired") and job["sandbox"]["state"] == "not_created")
                else "error"
            )
        )
    if any(name != path for name, path in paths.items()):
        save(backup / "paths.json", {"remote_to_local": paths})
    if asserted:
        outcome = "error"  # A closed unresolved launch is never lifecycle success.
    flush_backup_directories(backup, [*paths.values(), "paths.json"])
    save(
        backup.with_suffix(".verification.json"),
        {"files": hashes, "remote_to_local": paths, "complete_manifest": manifest is not None},
    )
    update(
        folder,
        job,
        training_outcome=training,
        supervisor_error=supervisor_error,
        runtime_timing=timing,
        gpu_metrics=manifest.get("gpu_metrics") if manifest else None,
        actual_gpu=(manifest.get("gpu_metrics") or {}).get("gpu_names") if manifest else None,
        backup={"path": str(backup), "files": len(files), "outcome": outcome},
        pending_backup=None,
        unstarted_backup=unstarted,
        no_workload_evidence={
            "protocol": 1,
            "terminal_observations": 2,
            "inventory": "exact uploaded files",
            "start_gate": "absent",
        }
        if never_started
        else (
            {"sandbox_create_not_sent": True, "inventory": "verified uploaded files"}
            if outcome == "no_capacity"
            else None
        ),
    )
    from_backup(folder, backup, paths)
    update(folder, job, phase="deleting storage", delete_requested=True)
    provider.delete_volume(volume_id)
    for _ in range(30):
        if not provider.volume_exists(volume_id):
            sleep(1)
            if not provider.volume_exists(volume_id):
                resource["state"] = "deleted"
                update(folder, job)
                return outcome
        sleep(1)
    raise LifecycleError("Volume deletion was requested but absence is not verified.")
