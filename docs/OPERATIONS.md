# Operations

Status separates the command's `outcome` from the stored `job_outcome`. Resource states and the
backup receipt remain authoritative evidence about what cleanup accomplished.

| Outcome            | Exit code |
| ------------------ | --------- |
| success            | 0         |
| error              | 1         |
| workload_failed    | 2         |
| no_capacity        | 4         |
| ambiguous          | 6         |
| cleanup_unverified | 7         |
| storage_retained   | 8         |
| detached           | 9         |
| cancelled          | 10        |
| pending            | 11        |
| partial_outputs    | 13        |
| wait_timeout       | 14        |
| usage_error        | 64        |
| interrupted        | 130       |

Unused numbers are reserved, not reassigned. `status` returns command success for a readable ledger
even if the job failed. `wait` returns its job's outcome, or its own timeout/interruption.

## Phases and resources

Normal phases are `arming watchdog`, `preparing image and app`, `building image`,
`creating storage`, `uploading inputs`, `waiting for container`, `running`,
`finalizing cancellation`, `stopping compute`, `verifying outputs`, `deleting storage` and
`finished`. Old ledgers may say `creating compute`. The eager image build is separate; container
waiting can still include mount loading and SDK work, so it is not a pure measurement of GPU queue
time. `running` means a Sandbox receipt exists; the committed `runtime_start` supplies stronger
evidence that the supervisor entered its run window.

`phase_history` records each phase entry as a Unix timestamp; repeated entries after recovery are
preserved. `phase_seconds` is time since the latest phase entry, including time since completion for
`finished`. `deadline_seconds_left` and `startup_seconds_left` never imply a deadline extension.
`watchdog.log` receives sanitized JSON lines for phase changes and observed errors, without SDK
message bodies or credentials. Log write failures cannot prevent cleanup.

Outcome publication still happens under the worker lock. Status exposes `worker_finishing: true`
while that lock remains held or its availability cannot be determined, even if `ended` is true.
`recovery_ready: true` means the existing lock was observed free, not a guarantee it stays free.
Wait for that field before recovery; recovery still acquires its own lock. The observer never
creates a lock file or changes ledger bytes.

A Sandbox is `not_created`, `unknown`, `exists`, `stopped` or `operator_asserted_absent`. The last
state is an explicit statement, not a terminal observation. A Volume is `not_created`, `unknown`,
`exists` or `deleted`. Unknown creation is not proof of absence. Accepted termination is not proof
of completion.

With a separate startup allowance, `startup_deadline` and the final `deadline` are fixed once at
launch. The runtime's committed start gate provides a bounded `run_deadline`; it cannot extend the
final deadline. Recovery never resets these values. The run budget includes finalization.

## Recovery

Use an explicit path or ID when `cancel latest` or `recover latest` reports a skipped, unreadable
ledger that is newer or cannot be dated. These commands refuse before touching another job.
Read-only observers continue to skip unreadable jobs with warnings. Selection uses immutable
`job.json` creation evidence, falling back to its modification time, never the active ledger's
mtime.

Use the exact ledger path when dealing with an interrupted job:

```sh
podvigil-modal status PATH_TO_LEDGER
podvigil-modal recover PATH_TO_LEDGER
```

A live worker holds the OS lock; recovery cannot take over. A rejected duplicate worker leaves the
owner's ledger unchanged. `watchdog_heard` reports process heartbeat freshness; `loop_heard` reports
control-loop progress separately. Outside the startup exception below, after 120 seconds without
progress `wait` stops even if the heartbeat thread is alive. A long API call or transfer may be
stalled; this is not proof of process death. `deadline_overdue` flags an unfinished controller past
its deadline. Observers write nothing.

Before blocking image build, Volume creation, upload or Sandbox creation, the worker records
`blocking_call` with `name`, `started_at` and `until`. `loop_heard` includes an unexpired call;
`loop_progress_recent` retains the raw 120-second freshness check. Image/Sandbox calls use the
remaining startup allowance, while Volume creation/upload use the fixed job deadline. These are
observation bounds, not new transport timeout guarantees. The record is cleared on return or error;
the heartbeat thread still never writes progress. A live heartbeat is required by `wait` throughout.

`startup_waiting` is an explicit exception to loop staleness during image preparation or a blocked
create, only before the fixed startup deadline (legacy: overall deadline). A fresh independent
heartbeat is still required. `wait` and launch do not report ambiguity just because scheduling
exceeds 120 seconds. After the allowance expires, unresolved creation remains ambiguous until
reconciled.

An ordinary failed poll is retried with bounded backoff until cancellation, the existing deadline,
or a successful terminal observation. It does not end healthy compute. Ownership refusals still fail
immediately. `observation_error` and `last_observed` distinguish uncertainty from a known state.

Start-marker reads follow the same rule. `runtime_start_error` records a failed read separately, so
successful Sandbox polls cannot hide it. At the startup deadline the worker takes a fresh marker
observation: confirmed absence ends startup, but a transport failure keeps monitoring within the
unchanged absolute deadline and native run timeout. Cancellation still takes effect. Ownership and
metadata validation refusals stop monitoring immediately; other read failures remain visible as
uncertainty.

The log mirror shares one allowance of at most five seconds across asynchronous Sandbox lookup,
ownership verification, execution and stream reads. A mirror timeout cannot bypass ownership checks
or keep its lookup running in an unbounded background thread.

If a worker died while compute still runs before its deadline, recovery refuses to stop it unless
`--stop-running` is explicit or a cancellation request already exists. A failed observation retains
that uncertainty. Once the deadline passes, recovery may stop the owned Sandbox. There is no
monitor-resume command; normal `wait` observes local records only.

A refusal to stop still-running compute returns command `cleanup_unverified` (7), with the refusal
text in the returned document. It does not save a terminal outcome or change the recorded phase.
`job_outcome` and `ended` therefore retain their prior values; a previously pending job remains
pending for `wait`. A newly ownership-verified Sandbox ID may still be saved. A failed preflight
status read also preserves the job record. This differs from an attempted cleanup whose termination
could not be verified, even though both commands use code 7; inspect the resource and job fields.

If compute is stopped but no complete manifest exists, normal recovery retains storage. The narrow
exception is a protocol-1 job with an owned Sandbox observed terminal twice, no
`runtime-start.json`, and exactly the receipted uploaded files with matching hashes. The supervisor
must commit that start gate before it may launch any workload. This evidence allows a verified
inputs-only backup and automatic deletion, returning `no_capacity` (4). It describes a start that
never reached workload execution, not a provider diagnosis of GPU stock. Plain `recover` uses the
same checks. Old ledgers without the protocol, missing/extra/changed files and unresolved creates
are not eligible. An explicit cancellation retains the `cancelled` outcome instead.
`recover --accept-partial` explicitly accepts copying and verifying every current file, then
deleting the Volume. It still refuses corrupt files, malformed manifests or ownership mismatches.

Cancellation first asks the owned supervisor to finalize. The grace period never extends the
original deadline. Failed signalling, failed observations or an unresponsive supervisor fall back to
hard termination. Check `stop_signal_error`, `sandbox_exit_code` and the final resource states.

Inputs and metadata that already exhaust recovery limits are refused before allocation. If compute
was definitely never requested, automatic cleanup verifies the remote files against saved upload
hashes, makes a durable backup, and deletes the receipted Volume. A partial upload is eligible only
when every present byte matches the upload receipt. An empty or owner-file-free upload additionally
needs the exclusive Volume-create acknowledgement. Unknown Sandbox creation, extra files and changed
bytes always retain storage. This conservative path still downloads and verifies data; it does not
assume that a staged local file remains intact.

Ordinary portable output names, including `epoch=1-step=100.ckpt`, keep their paths. If any path is
nonportable, too long, or conflicts by case, the entire backup uses hashed filenames under `files/`.
`paths.json` maps original remote paths to those files, and the verification receipt records the
same mapping. Never reconstruct untrusted paths without checking the destination filesystem.

New supervisors preserve symbolic-link targets as data in `output-links.json`, then remove the link
entries before manifest publication. Links are not followed or recreated locally; linked external
content is not an artifact. The metadata filename is reserved. Old Volumes that still contain links,
or any Volume containing unsupported special files, remain subject to manual export. A repeated
failed backup reuses its recorded directory only for the same names and sizes; all bytes are read
and checked again. Different inventories and older unrecorded backups are preserved.

The downloaded manifest must match the exact bytes parsed during validation. Even a same-size
manifest change retains storage; verifying later reads against a different downloaded manifest alone
would not prove that the backup matches the metadata used to approve cleanup.

A failed poll or ledger write does not block an ownership-checked termination attempt. Without two
consecutive terminal reads, cleanup remains unverified and no download or storage deletion is
permitted. Fix local disk failures before attempting backup again.

On POSIX, a directory-flush failure retains the cloud copy. If a crash happened after verified
Volume deletion but before the final outcome write, recovery restores the audited backup outcome
locally; it does not allocate or require a new provider session.

Unknown Volume creation without a successful create receipt requires operator investigation. A
successful exclusive create is saved as `create_acknowledged` before bounded ID-lookup retries. For
that acknowledged case, each `recover` may repeat the exact recorded name/environment lookup once. A
hit records the ID and its provenance, then follows normal ownership, inputs/empty-Volume and backup
checks. A miss or error leaves the Volume unresolved; it is never absence evidence. Deletion remains
by the recovered ID, never by name. A Sandbox may be reconciled with the exact intent and matching
ownership tags, or with `recover JOB --sandbox-id ID` after the same tag checks. The lookup tries
the SDK's running-GPU fallback after a terminated-inclusive miss. Two misses still mean unresolved,
not absent.

Never repeat an ambiguous create. In SDK 1.6.4, a `ResourceExhaustedError` can be raised after a
Sandbox was allocated while waiting for a GPU task. Exception names alone cannot prove rejection.
Only failure proven to occur before calling the create method is marked `not_created`.

The defensive `CreateNotSent` lookup branch is not a classifier for ordinary SDK create failures:
successful upload already caches the Volume handle. A quota error, invalid image error or timeout
does not establish that no Sandbox exists.

An explicit `recover JOB --assert-no-sandbox` can close unknown compute **only after** the recorded
deadline plus `timeout_seconds`, and only when a fresh ownership-checked lookup still finds nothing.
The refusal before that time reports when the option becomes available. It requires no recorded
Sandbox ID and cannot combine with `--sandbox-id` or `--stop-running`. Neither a lookup miss nor
elapsed time triggers this action automatically: the operator is asserting absence and accepting
that a pending resource might still start late.

The ledger records `operator_asserted_absent`, assertion time, eligibility time and the explicit
statement that it is not a provider observation. No terminal observations are invented. Inputs-only
Volumes require their owner file and the exact uploaded inventory, then use upload receipts for the
usual complete verified backup. The assertion never substitutes for Volume ownership, including
under `--accept-partial`. Complete manifests retain their checks; other missing manifests still need
deliberate `--accept-partial`, and corrupt/extra input data is not silently accepted. Deletion is
always by the receipted Volume ID after durable backup. Completed operator close returns `error`
(1), even if a manifest reports successful work; it cannot claim lifecycle success. Ordinary
`recover` can finish an interrupted close and checks again for an owned Sandbox that appeared later.
Any such match restores the owned-resource recovery path.

Project refusals retain their actionable text. Other exception messages are suppressed because SDK
errors may contain credentials or headers. `error.json` keeps the exception type and source frame
basenames/line numbers, without raw messages, source lines or local variables.
`supervisor-error.json` is a best-effort remote failure record, including a final sync failure after
manifest publication. Recovery permits this one root-level record to be absent from the manifest,
provided it is valid JSON containing only an `error_type` identifier and is at most 64 KiB. With
`--accept-partial`, an unreadable or invalid diagnostic is instead preserved as opaque bytes under
the same size limit and coverage exception. Without that flag, recovery names the file and refuses.
It is still backed up, hashed and independently read twice. `supervisor_error` in the ledger and
status contains either the valid diagnostic or a controlled unreadable notice, never the invalid
contents. Every manifest-listed file retains its hash check; other unlisted files still refuse
deletion. The manifest decides the outcome: a verified success stays success, while `not_started`
becomes `workload_failed`. The diagnostic is not an override or proof that no workload ran. A
manifest also includes periodic-sync diagnostics; a slow periodic commit does not abort training.
The final commit can use the whole remaining finalization window.

Stopped compute and deleted storage are different facts. Provider retention, logs and other charges
can outlive either. Review [operational limits](MODAL.md#operational-limits).

## Slow-start outcomes

For startup failures, callers should distinguish these three cases:

Read `failed_phase` alongside the outcome: `building image` means the image was not ready within the
startup allowance, before any Sandbox or Volume request; `waiting for container` means the Sandbox
start was attempted. Both can return `no_capacity` with appropriate no-start evidence. The
image-timeout message needs no recovery action because no Sandbox or Volume was requested.

| Outcome                | Evidence and next step                                                                                                                                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `no_capacity` (4)      | Recorded evidence proves the workload never started; any inputs-only Volume has been verified, backed up and deleted. A later launch is an explicit caller decision.                                            |
| `storage_retained` (8) | Compute is stopped, but evidence or a complete output manifest is missing. Inspect the backup/Volume; use ordinary recovery when sufficient evidence becomes available, or deliberately accept partial results. |
| `ambiguous` (6)        | A create remains unresolved. Reconcile ownership and receipts; never retry the create automatically.                                                                                                            |

If the new startup allowance expires before the Sandbox request is sent, the saved
`sandbox_create_not_sent` evidence replaces terminal observations of nonexistent compute. A Volume,
if created, still goes through the existing verified inputs-backup path. A protocol-1 supervisor
that enters after the overall deadline also exits before writing, even without a separate startup
allowance, and qualifies for the same verified no-start recovery. Jobs without the start protocol
retain their previous behavior. `no_capacity` is a no-start result, not proof that GPU stock was the
cause.

These are startup-failure distinctions, not the entire outcome table. A queued live worker remains
pending; detached launch returns 9. An owned Sandbox whose termination cannot be verified returns
`cleanup_unverified` (7). Explicit cancellation keeps its own outcome. No code or error class proves
an ambiguous create left nothing behind.

## Local observations and estimates

`latest` and ID prefixes skip unreadable or non-Modal ledgers and report each skipped path on
stderr. A directly named unreadable ledger still fails, and multiple healthy matches remain
ambiguous. Skipping a file does not modify it or establish that its cloud resources are absent.

Use `list` for all jobs in one state directory. Every readable ledger includes its id, creation
time, phase, outcome and estimated spend; an unreadable ledger gets an error row rather than hiding
other jobs. Unknown timing produces null spend, including old jobs and many incomplete starts.

`logs JOB --lines 100` reads only `workload-tail.json`. The worker uses its owned Sandbox session to
mirror at most 32768 bytes from each stdout/stderr file about every ten seconds, with a five-second
allowance covering lookup, ownership verification and exec/read, limited by remaining job time.
Mirror failure records `log_mirror_error`, preserves the last good tail and leaves training running.
The verified backup refreshes the final snapshot. The mirror is diagnostic data, not a recovery
receipt; deletion never relies on it.

Final `gpu_metrics` reports the mean across sampled device observations, the peak utilization of any
device and peak per-device memory in MiB. Missing samples produce null measurements. Sampling is
best effort every five seconds with a one-second command timeout; it is not continuous monitoring.
`actual_gpu` contains sampled device model names, not GPU identifiers or an inferred reservation.

Cold-driver discovery can take up to ten seconds before workload launch, bounded by the start and
run windows with a launch margin. Later sampling retains a one-second timeout; an unavailable
initial sample may leave `PODVIGIL_GPU` as `unknown` without failing the job.

`cost_estimate.sandbox_runtime_seconds` currently measures supervisor entry through manifest
preparation, excluding container boot and the final Volume commit. The field name does not imply
provider billing precision. `estimated_spend_usd` multiplies that measured interval by the user's
hourly quote. Both fields are null without a complete timing receipt. Builds, storage, egress and
unobserved compute are excluded. Check `duration_basis` and the provider invoice before comparing
actual cost; these values cannot establish that nothing was billed.
