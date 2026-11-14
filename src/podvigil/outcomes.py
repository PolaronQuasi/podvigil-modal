"""Stable Modal outcome codes. Gaps are reserved; existing codes are never reused."""

OUTCOMES = (
    ("success", 0, "Command succeeded; a successful job has verified backup and resource cleanup."),
    ("error", 1, "Command failed; inspect the ledger's resource states."),
    ("workload_failed", 2, "Workload failed or timed out; backup and cleanup are verified."),
    (
        "no_capacity",
        4,
        "An owned Sandbox ended before the workload start gate; inputs were verified, backed up and cleaned.",
    ),
    ("ambiguous", 6, "Resource creation is unresolved; it may exist and bill."),
    ("cleanup_unverified", 7, "Sandbox termination is not verified; compute may still bill."),
    ("storage_retained", 8, "Compute stopped; the Volume is retained and may bill."),
    ("detached", 9, "Launch returned while the independent worker owns the job."),
    ("cancelled", 10, "Cancellation completed with verified backup and cleanup."),
    ("pending", 11, "Cancellation requested; completion is not yet verified."),
    ("partial_outputs", 13, "Partial recovery was explicitly accepted, verified and cleaned up."),
    ("wait_timeout", 14, "Waiting expired; the job is unaffected."),
    ("usage_error", 64, "Invalid command arguments."),
    ("interrupted", 130, "Command interrupted; inspect the ledger before assuming cleanup."),
)
EXIT_CODES = {name: code for name, code, _ in OUTCOMES}
MEANINGS = {name: meaning for name, _, meaning in OUTCOMES}
