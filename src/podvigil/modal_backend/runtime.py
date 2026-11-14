"""Standalone Linux Sandbox entrypoint. Standard library only; no Modal credentials.

The trusted workload writes checkpoints directly to its dedicated Volume and shares
the supervisor's privilege boundary. Provider lifetime is the outer backstop.
"""

from pathlib import Path
import ctypes
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
import shutil
import math

CHECKPOINT_INTERVAL = 30


class GpuMonitor:
    """Best-effort standard-library samples; reserved output data, not workload credentials."""

    def __init__(self, output):
        self.executable = shutil.which("nvidia-smi")
        self.path = output / ".podvigil" / "gpu-samples.jsonl"
        self.next_sample = 0
        self.names, self.count, self.total = set(), 0, 0.0
        self.peak, self.memory, self.errors = None, None, 0
        try:
            self.path.parent.mkdir(exist_ok=True)
            self.path.touch(exist_ok=False)
        except OSError:
            self.errors += 1
            self.path = None

    def sample(self, *, timeout=1):
        if not self.executable or time.monotonic() < self.next_sample:
            return
        self.next_sample = time.monotonic() + 5
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "--query-gpu=name,utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
            devices = []
            for line in result.stdout.splitlines()[:8]:
                name, util, memory = [part.strip() for part in line.rsplit(",", 2)]
                util, memory = float(util), float(memory)
                if (
                    not name
                    or len(name) > 128
                    or not math.isfinite(util)
                    or not math.isfinite(memory)
                    or not 0 <= util <= 100
                    or memory < 0
                ):
                    raise ValueError("Invalid GPU sample")
                devices.append({"name": name, "utilization_percent": util, "memory_mib": memory})
            for device in devices:
                self.names.add(device["name"])
                self.count += 1
                self.total += device["utilization_percent"]
                self.peak = max(self.peak or 0, device["utilization_percent"])
                self.memory = max(self.memory or 0, device["memory_mib"])
            if self.path:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"at": time.time(), "devices": devices}) + "\n")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.errors += 1

    def summary(self):
        return {
            "available": bool(self.executable),
            "gpu_names": sorted(self.names),
            "sample_count": self.count,
            "mean_utilization_percent": self.total / self.count if self.count else None,
            "peak_utilization_percent": self.peak,
            "peak_memory_mib": self.memory,
            "sampling_errors": self.errors,
            "aggregation": "mean across device observations; peaks per device",
        }


def descendants():
    parents = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                parents[int(path.name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
    found, frontier = set(), {os.getpid()}
    while frontier:
        frontier = {
            pid for pid, parent in parents.items() if parent in frontier and pid not in found
        }
        found |= frontier
    return found


def quiesce():
    until = time.monotonic() + 5
    while True:
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        children = descendants()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() >= until:
            raise RuntimeError("Workload descendants did not end.")
        time.sleep(0.05)


def sync_volume(root, deadline, *, periodic=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("No time remains to commit the Volume.")
    subprocess.run(
        ["sync", str(root)], check=True, timeout=min(20, remaining) if periodic else remaining
    )


def preserve_links(root):
    """Record link targets as data, never follow or recreate links on the recovery host."""
    links = {}
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink():
                links[path.relative_to(root).as_posix()] = os.readlink(path)
    if not links:
        return
    metadata = root / "output-links.json"
    # Reserved metadata is never allowed to overwrite a workload's file.
    with metadata.open("x", encoding="utf-8") as stream:
        json.dump({"links": links, "materialized": False}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for name in links:
        (root / name).unlink()


def manifest(root, job_id, owner, outcome, returncode):
    files = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise RuntimeError("Outputs contain links or nonregular files.")
        name = path.relative_to(root).as_posix()
        if name in ("manifest.json", "manifest.tmp"):
            continue
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        files[name] = {"size": path.stat().st_size, "sha256": h.hexdigest()}
    return {
        "job_id": job_id,
        "owner": owner,
        "training_outcome": outcome,
        "returncode": returncode,
        "files": files,
    }


def runtime_window(job, wall_now, monotonic_now):
    if "startup_deadline" in job and wall_now >= job["startup_deadline"]:
        return None
    if job.get("start_protocol") == 1 and wall_now >= job["deadline"]:
        return None
    return monotonic_now + max(
        0, min(job["deadline"] - wall_now, job["config"].get("timeout_seconds", 86400))
    )


def initial_gpu_sample(monitor, job, finish_by):
    # Reserve launch overhead and keep discovery inside both startup and useful run time.
    allowance = min(
        10,
        job.get("startup_deadline", job["deadline"]) - time.time() - 0.25,
        finish_by - time.monotonic() - 0.25,
    )
    if allowance >= 0.1:
        monitor.sample(timeout=allowance)


def run(path):
    entered = time.monotonic()
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The Sandbox runtime requires Linux.")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("Cannot become the workload's child subreaper.")
    root = path.parent
    job = json.loads(path.read_text())
    spec = job["config"]
    started_at = time.time()
    deadline = runtime_window(job, started_at, time.monotonic())
    if deadline is None:
        return 75  # No Volume writes and no workload; the controller still verifies terminal state.
    # Once started, wall-clock adjustments must not extend this runtime's allowance.
    # Initial host clock skew remains bounded by Modal's independent Sandbox timeout.
    finish_by = deadline - spec["finalize_seconds"]
    if job.get("start_protocol") == 1:
        receipt = {
            "job_id": job["job_id"],
            "owner": job["owner"],
            "protocol": 1,
            "started_at": started_at,
            "run_deadline": min(job["deadline"], started_at + spec["timeout_seconds"]),
        }
        with (root / "runtime-start.json").open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        sync_volume(
            root,
            min(
                deadline,
                time.monotonic()
                + max(0, job.get("startup_deadline", job["deadline"]) - time.time()),
            ),
        )
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    (root / "inputs").mkdir(exist_ok=True)
    cancelled = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancelled.__setitem__(0, True))
    Path("/tmp/podvigil-supervisor.json").write_text(
        json.dumps({"pid": os.getpid(), "job_id": job["job_id"], "owner": job["owner"]})
    )
    environment = {
        k: v
        for k, v in os.environ.items()
        if k
        in (
            "PATH",
            "HOME",
            "LANG",
            "LD_LIBRARY_PATH",
            "CUDA_VISIBLE_DEVICES",
            "NVIDIA_VISIBLE_DEVICES",
            "NVIDIA_DRIVER_CAPABILITIES",
        )
    }
    environment.update(spec.get("workload_env", {}))
    environment.update(
        PODVIGIL_OUTPUT_DIR=str(output),
        PODVIGIL_INPUT_DIR=str(root / "inputs"),
        PYTHONUNBUFFERED="1",
    )
    gpu_monitor = GpuMonitor(output)
    initial_gpu_sample(gpu_monitor, job, finish_by)
    environment.update(
        PODVIGIL_GPU=",".join(sorted(gpu_monitor.names))
        or ("none" if not spec.get("gpu") else "unknown"),
        PODVIGIL_CPU=str(spec.get("cpu", 1)),
        PODVIGIL_MEMORY_MIB=str(spec.get("memory_mib", 0)),
        PODVIGIL_SECONDS_AVAILABLE=str(max(0, finish_by - time.monotonic())),
    )
    outcome, code = "not_started", None
    diagnostics = {}
    try:
        if time.monotonic() < finish_by and time.time() < job.get(
            "startup_deadline", job["deadline"]
        ):
            with (
                (output / "stdout.log").open("wb") as stdout,
                (output / "stderr.log").open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    spec["command"],
                    cwd=root / "inputs",
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                next_sync = time.monotonic() + CHECKPOINT_INTERVAL
                while process.poll() is None and time.monotonic() < finish_by and not cancelled[0]:
                    gpu_monitor.sample()
                    if time.monotonic() >= next_sync:
                        try:
                            sync_volume(root, finish_by, periodic=True)
                        except Exception as exc:
                            # A checkpoint commit may take longer than the periodic allowance.
                            # Keep running; the final commit gets the full reserved window.
                            diagnostics["periodic_sync_failures"] = (
                                diagnostics.get("periodic_sync_failures", 0) + 1
                            )
                            diagnostics["last_sync_error"] = type(exc).__name__
                        next_sync = time.monotonic() + CHECKPOINT_INTERVAL
                    time.sleep(0.1)
                code = process.poll()
                if code is None:
                    outcome = "cancelled" if cancelled[0] else "time_limit"
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=min(5, max(0.1, deadline - time.monotonic())))
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
                else:
                    outcome = "success" if code == 0 else "training_failed"
    except Exception as exc:
        diagnostics["runtime_error"] = type(exc).__name__
        outcome = "supervisor_failed"
    finally:
        quiesce()
    preserve_links(root)
    record = manifest(root, job["job_id"], job["owner"], outcome, code)
    record["diagnostics"] = diagnostics
    record["gpu_metrics"] = gpu_monitor.summary()
    record["timing"] = {
        "supervisor_started_at": started_at,
        "supervisor_seconds": max(0, time.monotonic() - entered),
    }
    if outcome == "success" and any(
        "outputs/" + name not in record["files"] for name in spec["required_artifacts"]
    ):
        record["training_outcome"] = "missing_artifacts"
    temp = root / "manifest.tmp"
    temp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(temp, root / "manifest.json")
    sync_volume(root, deadline)
    return 0 if record["training_outcome"] == "success" else 2


def main(path):
    try:
        return run(path)
    except Exception as exc:
        # Preserve the supervisor failure even when no final manifest could be published.
        # No raw exception messages, environment or workload command is copied here.
        try:
            with (path.parent / "supervisor-error.json").open("x", encoding="utf-8") as stream:
                json.dump({"error_type": type(exc).__name__}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass
        print("Sandbox runtime failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
