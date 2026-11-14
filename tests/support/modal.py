"""In-memory Modal service. No SDK, credentials, sockets or billable resources."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import time

from podvigil.records import save


def spec():
    return {
        "provider": "modal",
        "app": "test-app",
        "environment": "test",
        "image": "python:3.13-slim",
        "gpu": "L4",
        "cpu": 1,
        "memory_mib": 1024,
        "timeout_seconds": 90,
        "finalize_seconds": 10,
        "estimated_hourly_usd": 1,
        "max_compute_cost_usd": 0.1,
        # Keep dummy quotes strictly in the past even when Windows clocks round differently.
        "rate_checked_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "command": ["python3", "train.py"],
        "inputs": [],
        "required_artifacts": ["result.txt"],
        "block_network": False,
        "max_backup_bytes": 1024**2,
        "max_backup_files": 100,
    }


def job(folder, config=None):
    record = {
        "provider": "modal",
        "job_id": "a" * 32,
        "owner": "b" * 32,
        "phase": "armed",
        "heartbeat": time.time(),
        "config": config or spec(),
        "sandbox": {"state": "not_created", "name": "test-sandbox"},
        "volume": {"state": "not_created", "name": "test-volume"},
    }
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / "modal-job.json", record)
    return record


class FakeModal:
    def __init__(self):
        self.calls = []
        self.data = {}
        self.failure = None
        self.running = True
        self.deleted = False
        self.finish = True
        self.termination_works = True
        self.deletion_works = True
        self.training_outcome = "success"
        self.publish_manifest = True
        self.hook = lambda name: None

    def call(self, name):
        self.calls.append(name)
        self.hook(name)
        if name == self.failure:
            raise RuntimeError("simulated secret must not be recorded")

    def prepare(self):
        self.call("prepare")

    def build_image(self, allowance):
        self.call("build_image")

    def create_volume(self, name, *, acknowledged=lambda: None):
        self.call("create_volume")
        acknowledged()
        return "vo-test"

    def upload(self, volume_id, files):
        self.call("upload")
        self.data.update(
            {
                name: value if isinstance(value, bytes) else Path(value).read_bytes()
                for name, value in files.items()
            }
        )

    def create_sandbox(self, record, remaining):
        self.call("create_sandbox")
        self.timeout = remaining
        if record.get("start_protocol") == 1:
            self.data["runtime-start.json"] = json.dumps(
                {
                    "job_id": record["job_id"],
                    "owner": record["owner"],
                    "protocol": 1,
                    "started_at": time.time(),
                    "run_deadline": record["deadline"],
                }
            ).encode()
        return "sb-test"

    def runtime_info(self, record):
        self.call("runtime_info")
        return (
            json.loads(self.data["runtime-start.json"])
            if "runtime-start.json" in self.data
            else None
        )

    def publish(self, record):
        self.data["outputs/result.txt"] = b"trained locally in a fake"
        if self.publish_manifest:
            files = {
                name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in self.data.items()
            }
            self.data["manifest.json"] = json.dumps(
                {
                    "job_id": record["job_id"],
                    "owner": record["owner"],
                    "training_outcome": self.training_outcome,
                    "returncode": 0,
                    "files": files,
                }
            ).encode()

    def poll(self, record):
        self.call("poll")
        if self.running and self.finish:
            self.publish(record)
            self.running = False
        return None if self.running else 0

    def log_tail(self, record, allowance=5):
        self.call("log_tail")
        return {"stdout": "fake workload output\n", "stderr": ""}

    def terminate(self, record):
        self.call("terminate")
        if self.termination_works:
            self.running = False

    def resolve_sandbox(self, record):
        self.call("resolve_sandbox")
        return None

    def resolve_volume(self, record):
        self.call("resolve_volume")
        return None

    def files(self, volume_id):
        self.call("files")
        return {k: len(v) for k, v in self.data.items()}

    def read_file(self, volume_id, name):
        self.call("read:" + name)
        yield self.data[name]

    def delete_volume(self, volume_id):
        self.call("delete_volume")
        if self.deletion_works:
            self.deleted = True

    def volume_exists(self, volume_id):
        self.call("volume_exists")
        return not self.deleted
