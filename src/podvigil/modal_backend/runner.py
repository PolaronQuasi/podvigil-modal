"""Independent Modal job ownership, launch and recovery; no allocation retry loop."""

from pathlib import Path
from datetime import datetime, timezone
import json
import hashlib
import math
import sys
import threading
import time
import uuid

from ..watchdog_spawn import spawn
from ..identity import identity
from ..records import best_effort_save, exclusive_lock, read, save, utc
from .provider import Provider, credential_proof
from .recovery import completed_outcome, recover_outputs, stop_compute
from .spec import check_quote, read_spec, stage
from .state import best_effort_update, jobs_dir, load, remaining_outcome, status, update, wait
from .errors import UserError, LifecycleError, CreateNotSent, job_lock, record_error, safe_error
from . import logs

IMAGE_START_EXPIRED = (
    "The image was not ready within the startup allowance; "
    "no Sandbox or Volume resources were requested."
)


def launch(path, *, detach=False, progress=lambda line: None):
    spec, sources = read_spec(path)
    check_quote(spec)
    salt = uuid.uuid4().hex
    proof = credential_proof(salt)  # Local config only, before making a ledger or any API call.
    job_id = uuid.uuid4().hex
    folder = jobs_dir() / job_id
    folder.mkdir(parents=True)
    folder = folder.resolve()
    job = {
        "provider": "modal",
        "runner": identity(),
        "job_id": job_id,
        "created_utc": utc(),
        "owner": uuid.uuid4().hex,
        "phase": "arming watchdog",
        "heartbeat": time.time(),
        "config": spec,
        "credential_salt": salt,
        "credential_proof": proof,
        "sandbox": {"state": "not_created", "name": "pv-" + job_id},
        "volume": {"state": "not_created", "name": "pv-" + job_id},
    }
    set_start_deadline(job, time.time())
    save(folder / "job.json", {k: job[k] for k in ("provider", "job_id", "created_utc")})
    update(folder, job)
    try:
        update(folder, job, staged_inputs=stage(sources, folder))
        # Empty input folders must exist as a workload working directory too.
        (folder / "inputs").mkdir(exist_ok=True)
        code = "import runpy,sys;sys.path.insert(0,{!r});runpy.run_module('podvigil.modal_backend.worker',run_name='__main__')".format(
            str(Path(__file__).resolve().parents[2])
        )
        monitor = spawn(
            folder,
            [sys.executable, "-B", "-c", code, str(folder)],
            credential_proof,
            require_same_credentials=True,
        )
        monitor.close()
        save(folder / "release.json", {"released": True})
    except BaseException as exc:
        record_error(folder, exc)
        # release.json may already be visible even if its durable save was interrupted.
        # Never publish the parent's stale pre-allocation snapshot over the worker's receipts.
        best_effort_save(folder / "cancel.json", {"requested": True})
        try:
            with exclusive_lock(folder / "modal-worker.lock", timeout=0):
                current = load(folder)
                if (
                    not current.get("launch_started")
                    and not current.get("outcome")
                    and all(
                        current[name]["state"] == "not_created" for name in ("sandbox", "volume")
                    )
                ):
                    best_effort_update(
                        folder,
                        current,
                        phase="finished",
                        outcome="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                        error=safe_error(exc),
                    )
        except TimeoutError:
            pass  # The active worker exclusively owns its status and resource receipts.
        if isinstance(exc, KeyboardInterrupt):
            return status(folder), "interrupted"
        return status(folder), "error"
    progress("Modal ledger: " + str(folder))
    try:
        if detach:
            result = status(folder)
            return result, result["outcome"] if result["ended"] else "detached"
        return wait(folder, progress=progress)
    except KeyboardInterrupt:
        save(folder / "cancel.json", {"requested": True})
        progress("Launch interrupted; cancellation requested. The watchdog still owns cleanup.")
        return status(folder), "interrupted"


def _check_identity(job):
    if credential_proof(job["credential_salt"]) != job["credential_proof"]:
        raise LifecycleError("Modal credentials differ from those that launched this job.")


def set_start_deadline(job, now):
    """Set once, before staging/arming for new launches; never reset during recovery."""
    if "startup_timeout_seconds" in job["config"] and "deadline" not in job:
        job["startup_deadline"] = now + job["config"]["startup_timeout_seconds"]
        job["deadline"] = job["startup_deadline"] + job["config"]["timeout_seconds"]


def execute(folder, provider, *, clock=time.time, sleep=time.sleep):
    """One worker owns this lifecycle. Callers hold the job's OS lock."""
    job = load(folder)
    if (
        job.get("launch_started")
        or job.get("outcome")
        or any(job[name]["state"] != "not_created" for name in ("sandbox", "volume"))
    ):
        raise LifecycleError("An existing Modal attempt cannot be launched again; use recovery.")
    set_start_deadline(job, clock())
    update(folder, job, launch_started=True, start_protocol=1)
    try:
        if (folder / "cancel.json").exists():
            update(folder, job, phase="finished", outcome="cancelled")
            return
        check_quote(job["config"], clock())
        update(folder, job, phase="preparing image and app")
        provider.prepare()
        if (folder / "cancel.json").exists():
            update(folder, job, phase="finished", outcome="cancelled")
            return
        # The fixed overall allowance begins before the first billable resource request.
        if "deadline" not in job:
            update(folder, job, deadline=clock() + job["config"]["timeout_seconds"])
        update(
            folder,
            job,
            phase="building image",
            blocking_call={
                "name": "build_image",
                "started_at": clock(),
                "until": job.get("startup_deadline", job["deadline"]),
            },
        )
        provider.build_image(job.get("startup_deadline", job["deadline"]) - clock())
        update(folder, job, blocking_call=None)
        if (
            clock() >= job.get("startup_deadline", job["deadline"])
            or (folder / "cancel.json").exists()
        ):
            update(
                folder,
                job,
                phase="finished",
                outcome="cancelled"
                if (folder / "cancel.json").exists()
                else ("no_capacity" if "startup_deadline" in job else "error"),
                no_workload_evidence={
                    "sandbox_create_not_sent": True,
                    "volume_create_not_sent": True,
                },
                error=IMAGE_START_EXPIRED
                if "startup_deadline" in job and not (folder / "cancel.json").exists()
                else "Startup ended before resource creation.",
                failed_phase="building image",
            )
            return
        data = {
            "owner.json": json.dumps({"job_id": job["job_id"], "owner": job["owner"]}).encode(),
            "run.json": json.dumps(
                {
                    k: job[k]
                    for k in (
                        "job_id",
                        "owner",
                        "config",
                        "deadline",
                        "start_protocol",
                        "startup_deadline",
                    )
                    if k in job
                }
            ).encode(),
        }
        data.update(
            {"inputs/" + name: folder / "inputs" / name for name in job["config"]["inputs"]}
        )
        upload_manifest = dict(job.get("staged_inputs", {}))
        upload_manifest.update(
            {
                name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for name, value in data.items()
                if isinstance(value, bytes)
            }
        )
        if set(upload_manifest) != set(data):
            raise UserError("Input staging receipts are missing; no resources were requested.")
        if (
            sum(item["size"] for item in upload_manifest.values())
            >= job["config"]["max_backup_bytes"]
        ):
            raise UserError(
                "Staged inputs and metadata exhaust max_backup_bytes; no resources were requested."
            )
        update(folder, job, upload_manifest=upload_manifest)
        job["volume"]["state"] = "unknown"
        update(
            folder,
            job,
            phase="creating storage",
            blocking_call={
                "name": "create_volume",
                "started_at": clock(),
                "until": job["deadline"],
            },
        )

        def volume_acknowledged():
            job["volume"]["create_acknowledged"] = True
            update(folder, job)

        volume_id = provider.create_volume(job["volume"]["name"], acknowledged=volume_acknowledged)
        job["volume"].update(state="exists", id=volume_id)
        update(folder, job, blocking_call=None)
        update(
            folder,
            job,
            phase="uploading inputs",
            blocking_call={"name": "upload", "started_at": clock(), "until": job["deadline"]},
        )
        provider.upload(volume_id, data)
        update(folder, job, blocking_call=None)
        remaining = math.floor(job.get("startup_deadline", job["deadline"]) - clock())
        minimum = 0 if "startup_deadline" in job else job["config"]["finalize_seconds"] + 5
        if remaining <= minimum or (folder / "cancel.json").exists():
            job["cancel_requested"] = (folder / "cancel.json").exists()
            raise LifecycleError(
                "Launch was cancelled or its allowance expired before Sandbox creation."
            )
        job["sandbox"]["state"] = "unknown"
        update(
            folder,
            job,
            phase="waiting for container",
            blocking_call={
                "name": "create_sandbox",
                "started_at": clock(),
                "until": min(job.get("startup_deadline", job["deadline"]), clock() + remaining),
            },
        )
        try:
            sandbox_id = provider.create_sandbox(job, remaining)
        except CreateNotSent:
            job["sandbox"]["state"] = "not_created"
            update(folder, job)
            raise
        job["sandbox"].update(state="exists", id=sandbox_id)
        update(folder, job, phase="running", blocking_call=None)
        monitor(provider, folder, job, clock=clock, sleep=sleep)
        outcome = recover_outputs(provider, folder, job, sleep=sleep)
        update(folder, job, phase="finished", outcome=outcome)
    except BaseException as exc:
        record_error(folder, exc)
        # Termination is still attempted if publishing, reads or recovery failed. Unknown creates
        # are never repeated or inferred from a matching name. Keep the Volume on uncertainty.
        failed_phase = job["phase"]
        startup_expired = "startup_deadline" in job and clock() >= job["startup_deadline"]
        best_effort_update(
            folder,
            job,
            startup_expired=startup_expired,
            blocking_call=None,
            cancel_requested=job.get("cancel_requested", False)
            or (folder / "cancel.json").exists(),
        )
        try:
            stop_compute(provider, folder, job, sleep=sleep)
        except BaseException:
            pass
        cleaned_outcome = None
        if startup_expired and all(
            job[name]["state"] == "not_created" for name in ("sandbox", "volume")
        ):
            best_effort_update(
                folder,
                job,
                no_workload_evidence={
                    "sandbox_create_not_sent": True,
                    "volume_create_not_sent": True,
                },
            )
            cleaned_outcome = "cancelled" if job.get("cancel_requested") else "no_capacity"
        if job["volume"]["state"] == "exists" and (
            job["sandbox"]["state"] == "not_created"
            or (
                job["sandbox"]["state"] == "stopped"
                and failed_phase in ("creating compute", "waiting for container")
            )
        ):
            try:
                cleaned_outcome = recover_outputs(
                    provider,
                    folder,
                    job,
                    unstarted=job["sandbox"]["state"] == "not_created",
                    require_no_start=job["sandbox"]["state"] == "stopped",
                    sleep=sleep,
                )
            except Exception as cleanup_exc:
                best_effort_update(folder, job, cleanup_error=safe_error(cleanup_exc))
        update(
            folder,
            job,
            phase="finished",
            outcome=cleaned_outcome or remaining_outcome(job),
            error=IMAGE_START_EXPIRED
            if cleaned_outcome == "no_capacity" and failed_phase == "building image"
            else safe_error(exc),
            failed_phase=failed_phase,
        )


def recover(
    folder,
    *,
    accept_partial=False,
    stop_running=False,
    sandbox_id=None,
    assert_no_sandbox=False,
    provider=None,
):
    with job_lock(folder):
        job = load(folder)
        if assert_no_sandbox and (
            sandbox_id
            or stop_running
            or job["sandbox"]["state"] != "unknown"
            or job["sandbox"].get("id")
        ):
            raise LifecycleError(
                "--assert-no-sandbox requires unknown compute without an ID, and cannot be combined with --sandbox-id or --stop-running."
            )
        restored = completed_outcome(job)
        if restored:
            update(folder, job, phase="finished", outcome=restored, error=None)
            return status(folder), restored
        if (
            job.get("outcome")
            and job["sandbox"]["state"] in ("not_created", "stopped")
            and job["volume"]["state"] in ("not_created", "deleted")
        ):
            return status(folder), job["outcome"]
        if provider is None:
            _check_identity(job)
            provider = Provider(job["config"])
        if sandbox_id:
            if job["sandbox"].get("id") not in (None, sandbox_id):
                raise LifecycleError(
                    "The supplied Sandbox ID conflicts with this ledger's receipt."
                )
            candidate = {**job, "sandbox": {**job["sandbox"], "id": sandbox_id}}
            provider.sandbox(candidate)  # Both ownership tags must match before accepting the ID.
            job["sandbox"].update(
                state="exists", id=sandbox_id, evidence="operator ID and both ownership tags"
            )
            update(folder, job)
        if assert_no_sandbox:
            deadline = job.get("deadline")
            timeout = job["config"].get("timeout_seconds")
            if (
                any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in (deadline, timeout)
                )
                or timeout <= 0
            ):
                raise LifecycleError(
                    "An operator close requires the recorded fixed deadline and Sandbox timeout."
                )
            eligible = deadline + timeout
            if time.time() <= eligible:
                when = datetime.fromtimestamp(eligible, timezone.utc).isoformat()
                raise LifecycleError(
                    "--assert-no-sandbox is available only after "
                    + when
                    + "; elapsed time is not absence evidence."
                )
            found = provider.resolve_sandbox(job)
            if found:
                job["sandbox"].update(
                    state="exists", id=found, evidence="creation intent and both ownership tags"
                )
                update(folder, job)
                raise LifecycleError(
                    "An owned Sandbox was found; the no-Sandbox assertion is refused. Inspect or recover that resource."
                )
            job["sandbox"].update(
                state="operator_asserted_absent",
                operator_assertion={
                    "source": "operator",
                    "at": time.time(),
                    "eligible_after": eligible,
                    "lookup_result": "not_found",
                    "statement": "The operator asserts no Sandbox exists. This is an operator statement, not an observation of absence or stopped compute. A pending Sandbox could still start late.",
                },
            )
            update(folder, job)
        elif job["sandbox"]["state"] == "operator_asserted_absent":
            # A later recovery must not ignore an owned Sandbox that has appeared since the assertion.
            found = provider.resolve_sandbox(job)
            if found:
                job["sandbox"].update(
                    state="exists",
                    id=found,
                    evidence="creation intent and both ownership tags after operator assertion",
                )
                update(folder, job)
        # This path only reads, terminates and recovers. It never calls create or extends deadlines.
        try:
            if job["sandbox"]["state"] == "unknown":
                found = provider.resolve_sandbox(job)
                if found:
                    job["sandbox"].update(
                        state="exists", id=found, evidence="creation intent and both ownership tags"
                    )
                    update(folder, job)
                else:
                    raise LifecycleError(
                        "Sandbox creation is unresolved; use --sandbox-id with an owned resource ID or investigate the provider. No storage is deleted."
                    )
            if (
                job["sandbox"]["state"] == "exists"
                and not stop_running
                and not (folder / "cancel.json").exists()
                and time.time() < job.get("deadline", float("inf"))
            ):
                try:
                    running = provider.poll(job) is None
                except Exception as exc:
                    return {**status(folder), "error": safe_error(exc)}, "cleanup_unverified"
                if running:
                    return {
                        **status(folder),
                        "error": "The Sandbox is still running. Use recover --stop-running to stop it, or wait for it to finish. The saved phase and outcome are unchanged.",
                    }, "cleanup_unverified"
            best_effort_update(folder, job, outcome=None)
            if (stop_running or (folder / "cancel.json").exists()) and job["sandbox"][
                "state"
            ] == "exists":
                job["cancel_requested"] = True
                graceful_stop(provider, folder, job)
            outcome = recover_outputs(
                provider,
                folder,
                job,
                accept_partial=accept_partial,
                unstarted=job["sandbox"]["state"] == "not_created",
            )
            update(folder, job, phase="finished", outcome=outcome, error=None)
        except Exception as exc:
            record_error(folder, exc)
            update(
                folder, job, phase="finished", outcome=remaining_outcome(job), error=safe_error(exc)
            )
        return status(folder), job["outcome"]


def monitor(provider, folder, job, *, clock=time.time, sleep=time.sleep):
    """A failed observation is uncertainty, not a request to stop healthy compute."""
    failures = 0
    next_observation = 0
    next_logs = 0
    while True:
        best_effort_update(folder, job)
        if (folder / "cancel.json").exists():
            best_effort_update(folder, job, cancel_requested=True)
            graceful_stop(provider, folder, job, clock=clock, sleep=sleep)
            return
        if clock() >= min(job["deadline"], job.get("run_deadline", job["deadline"])):
            return
        if "startup_deadline" in job and not job.get("runtime_start"):
            if clock() >= next_observation:
                observed_at = clock()
                try:
                    receipt = provider.runtime_info(job)
                    best_effort_update(folder, job, runtime_start_error=None)
                    if receipt:
                        best_effort_update(
                            folder,
                            job,
                            runtime_start=receipt,
                            run_deadline=min(job["deadline"], receipt["run_deadline"]),
                        )
                    elif observed_at >= job["startup_deadline"]:
                        # Only a fresh missing-marker read after the start allowance can
                        # end startup. A failed read cannot prove that no workload started.
                        best_effort_update(folder, job, startup_expired=True)
                        return
                except UserError:
                    raise  # Invalid ownership or metadata is not a transport outage.
                except Exception as exc:
                    if safe_error(exc) != job.get("runtime_start_error"):
                        record_error(folder, exc)
                    best_effort_update(folder, job, runtime_start_error=safe_error(exc))
                next_observation = (
                    min(clock() + 5, job["startup_deadline"])
                    if observed_at < job["startup_deadline"]
                    else clock() + 5
                )
        try:
            code = provider.poll(job)
            failures = 0
            best_effort_update(folder, job, observation_error=None, last_observed=clock())
            if code is not None:
                best_effort_update(folder, job, sandbox_exit_code=code)
                return
        except UserError:
            raise  # An ownership refusal is not a transient transport error.
        except Exception as exc:
            failures += 1
            if safe_error(exc) != job.get("observation_error"):
                record_error(folder, exc)
            best_effort_update(folder, job, observation_error=safe_error(exc))
        limit = min(job["deadline"], job.get("run_deadline", job["deadline"]))
        if not job.get("runtime_start") and clock() < job.get("startup_deadline", 0):
            limit = min(limit, job["startup_deadline"])
        if failures == 0 and clock() >= next_logs and limit > clock():
            try:
                streams = provider.log_tail(job, allowance=min(5, limit - clock()))
                logs.publish(folder, streams, source="running Sandbox")
                best_effort_update(folder, job, log_mirror_error=None)
            except Exception as exc:
                if safe_error(exc) != job.get("log_mirror_error"):
                    record_error(folder, exc)
                best_effort_update(folder, job, log_mirror_error=safe_error(exc))
            next_logs = clock() + 10
        sleep(min(min(30, 2 ** min(failures + 1, 5)), max(0, limit - clock())))


def graceful_stop(provider, folder, job, *, clock=time.time, sleep=time.sleep):
    until = min(job.get("deadline", clock()), clock() + job["config"]["finalize_seconds"])
    if until <= clock():
        return
    try:
        provider.request_stop(job)
    except Exception as exc:
        record_error(folder, exc)
        best_effort_update(folder, job, stop_signal_error=safe_error(exc))
        return
    best_effort_update(folder, job, phase="finalizing cancellation")
    while clock() < until:
        try:
            code = provider.poll(job)
            if code is not None:
                best_effort_update(folder, job, sandbox_exit_code=code)
                return
        except Exception:
            pass
        sleep(min(1, max(0, until - clock())))
        best_effort_update(folder, job)


def heartbeat(folder, stopped):
    while not stopped.is_set():
        try:
            save(folder / "modal-heartbeat.json", {"at": time.time()})
        except OSError:
            pass
        stopped.wait(5)


def worker(folder):
    from ..watchdog_spawn import arm

    with exclusive_lock(folder / "modal-worker.lock", timeout=0):
        try:
            _owned_worker(folder, arm)
        except BaseException as exc:
            record_error(folder, exc)
            # Only the lock owner may publish a failure. A rejected duplicate writes nothing.
            job = load(folder)
            if not job.get("outcome"):
                best_effort_update(
                    folder,
                    job,
                    phase="finished",
                    outcome=remaining_outcome(job),
                    error=safe_error(exc),
                )
            raise


def _owned_worker(folder, arm):
    # The caller holds modal-worker.lock throughout this lifecycle.
    arm(folder, credential_proof)
    end = time.monotonic() + 30
    while not (folder / "release.json").exists():
        if (folder / "cancel.json").exists() or time.monotonic() >= end:
            job = load(folder)
            update(
                folder,
                job,
                phase="finished",
                outcome="error",
                error="Controller did not release the armed watchdog; no resources created.",
            )
            return
        time.sleep(0.05)
    job = load(folder)
    _check_identity(job)
    stopped = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(folder, stopped), daemon=True)
    thread.start()
    try:
        execute(folder, Provider(job["config"]))
    finally:
        stopped.set()
        thread.join(timeout=2)
