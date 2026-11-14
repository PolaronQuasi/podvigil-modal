"""Diagnostics whose wording is controlled by this project, never by the cloud SDK."""

from pathlib import Path
from contextlib import contextmanager, ExitStack
import traceback

from ..records import best_effort_save, exclusive_lock
from .events import event


class UserError(ValueError):
    """An actionable refusal safe to show without exposing third-party exception text."""


class LifecycleError(UserError, RuntimeError):
    """A lifecycle precondition failed."""


class LockBusy(UserError, TimeoutError):
    """Another worker owns the ledger."""


class CreateNotSent(LifecycleError):
    """Local preparation failed before the Sandbox create SDK method was called."""


@contextmanager
def job_lock(folder):
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(Path(folder) / "modal-worker.lock", timeout=0))
        except TimeoutError:
            raise LockBusy(
                "Another worker holds this job lock. Inspect status or wait; recovery cannot take over a live worker."
            ) from None
        yield


def safe_error(exc):
    if isinstance(exc, UserError):
        return str(exc)
    return (
        "Modal operation failed ("
        + type(exc).__name__
        + "); inspect resource states and recover this ledger."
    )


def record_error(folder, exc):
    event(folder, "error", safe_error(exc))
    # Raw SDK tracebacks can include tokens, URLs and headers. Preserve frame locations,
    # exception types and our own safe diagnostic, never third-party messages or locals.
    best_effort_save(
        Path(folder) / "error.json",
        {
            "type": type(exc).__name__,
            "message": safe_error(exc),
            "frames": [
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(exc.__traceback__)
            ],
        },
    )
