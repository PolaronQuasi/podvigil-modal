"""Best-effort, single-line local diagnostics. Call only with project-controlled messages."""

from pathlib import Path
import json

from ..records import utc


def event(folder, kind, message):
    try:
        with (Path(folder) / "watchdog.log").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"at": utc(), "event": kind, "message": message}, ensure_ascii=True)
                + "\n"
            )
    except OSError:
        pass  # A log cannot become a prerequisite for cleanup.
