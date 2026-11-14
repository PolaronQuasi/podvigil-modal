"""Detached entrypoint; never launched implicitly by an observer."""

from pathlib import Path
import sys

from .runner import worker


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    try:
        worker(folder)
    except BaseException:
        # Failure reporting belongs inside runner.worker's ownership lock.
        raise SystemExit(1)
