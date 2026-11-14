# Lifecycle contract

The sole provider is Modal. Each launch owns at most one Sandbox and one Volume.

1. Validate configuration and cost estimates locally. Arm the detached worker with matching
   credential fingerprints before releasing cloud access.
2. Persist creation intent before issuing each request. An ambiguous request is never repeated.
3. Store returned IDs and verify Sandbox ownership tags before termination.
4. Maintain a fixed deadline; recovery never creates compute or extends it. With an explicit startup
   allowance, set the deadline once at launch to startup plus run allowances. The supervisor and
   local worker bound the run by its run allowance, even when startup is early. Modal's independent
   timeout instead uses whole seconds remaining until the fixed deadline at create, rounded down and
   capped at 86400. It is not a separate native run-allowance limit. If startup plus run exceeds 24
   hours, that cap can end the container before the full run/finalization window. Provider timer
   start semantics still require live validation; the local fixed deadline is never extended.
5. Retry transient observations without cancelling healthy work. Observability failure must not
   prevent termination when cancellation or a deadline requires it. Only two consecutive terminal
   observations establish stopped compute; failed observations do not count.
6. Verify Volume ownership, a complete inventory, sizes, SHA-256 hashes, local bytes and independent
   remote reads. Missing or inconsistent evidence retains storage. A protocol-backed no-workload
   start still requires verified terminal compute and a complete, verified inputs-only backup before
   storage deletion or the `no_capacity` outcome.
7. Flush files and POSIX directory entries before saving backup verification and deleting storage.
   Deletion is by receipted ID and needs two absence observations.
8. The OS lock protects lifecycle writes. A rejected duplicate worker cannot publish status. The
   parent never overwrites worker receipts after release.
9. Recover the audited backup outcome if a crash interrupted final outcome publication.
10. Observer commands do not call Modal or change records. `latest` uses immutable creation
    evidence, never a running job's last modification time.
11. Recovery before the deadline requires explicit consent to stop running compute. Cancellation
    allows bounded finalization, then hard termination; neither extends the existing deadline.

Images and workloads are trusted. Finite provider timeouts and local safeguards do not guarantee the
final invoice. Retained Volumes may continue to bill. See [Modal](docs/MODAL.md).
