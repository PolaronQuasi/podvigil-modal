"""Bounded local log snapshots; only the lifecycle worker publishes them."""

from pathlib import Path
import time

from ..records import best_effort_save, read

TAIL_BYTES = 32768


def publish(folder, streams, *, source):
    if not isinstance(streams, dict) or any(
        not isinstance(streams.get(name), str) for name in ("stdout", "stderr")
    ):
        raise ValueError("Invalid workload log snapshot")
    return best_effort_save(
        Path(folder) / "workload-tail.json",
        {
            "mirrored_at": time.time(),
            "source": source,
            **{name: streams[name][-TAIL_BYTES:] for name in ("stdout", "stderr")},
        },
    )


def from_backup(folder, backup, paths):
    streams = {}
    try:
        for name in ("stdout", "stderr"):
            relative = paths.get("outputs/" + name + ".log")
            if relative is None:
                streams[name] = ""
                continue
            with (backup / relative).open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - TAIL_BYTES))
                streams[name] = stream.read(TAIL_BYTES).decode("utf-8", errors="replace")
        if any("outputs/" + name + ".log" in paths for name in ("stdout", "stderr")):
            publish(folder, streams, source="verified backup")
    except OSError:
        pass  # Observability must not block already verified cleanup.


def snapshot(folder, lines=100):
    record = read(Path(folder) / "workload-tail.json", None)
    if record is None:
        return {"available": False, "note": "No workload log snapshot has been mirrored yet."}
    return {
        "available": True,
        "mirrored_at": record["mirrored_at"],
        "age_seconds": max(0, time.time() - record["mirrored_at"]),
        "source": record["source"],
        "stdout": "\n".join(record["stdout"].splitlines()[-lines:]),
        "stderr": "\n".join(record["stderr"].splitlines()[-lines:]),
        "note": "Bounded cached tail, not a live cloud read; use the backup for complete logs.",
    }
