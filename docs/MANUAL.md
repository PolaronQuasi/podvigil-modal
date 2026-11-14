# Manual

Install in a dedicated virtual environment with `python -m pip install .`. Configure credentials
with `modal token new`; on Windows use the local SDK profile so the independent worker can resolve
the same credentials.

The command is `podvigil-modal [--state-dir ROOT] COMMAND`. `python -m podvigil` is equivalent. The
only provider is Modal. The default state directory is the user's `podvigil-modal` application data
directory; `PODVIGIL_MODAL_STATE_DIR` overrides it. Ledgers live below `ROOT/modal/jobs`.

| Command                                                                                   | Behavior                                                                                                      |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `plan CONFIG`                                                                             | Validate and estimate offline; no credentials or state writes.                                                |
| `launch CONFIG [--detach]`                                                                | Arm the worker and launch one managed attempt.                                                                |
| `status JOB`                                                                              | Print the local status document; exit 0 when readable.                                                        |
| `list`                                                                                    | List every local job with creation time, phase, outcome and estimated spend.                                  |
| `logs JOB [--lines N]`                                                                    | Read the worker's cached stdout/stderr tail; default 100 lines per stream.                                    |
| `wait JOB [--timeout MINUTES]`                                                            | Observe until completion, stalled worker or timeout.                                                          |
| `cancel JOB`                                                                              | Write a cancellation request; does not claim termination.                                                     |
| `recover JOB [--accept-partial] [--stop-running] [--sandbox-id ID] [--assert-no-sandbox]` | Lock an existing job and recover its Volume; early stop or an operator assertion requires an explicit option. |

`JOB` accepts a ledger path, `latest`, or a unique hexadecimal ID prefix of at least four
characters. `latest` means newest launch by creation time, not last update. Older ledgers use their
immutable `job.json` sentinel timestamp. Full ledger paths remain usable across changes to the
installation or default state directory.

Observers skip unreadable ledgers with a warning. `cancel latest` and `recover latest` refuse if a
skipped job may be newer: its immutable `job.json` creation timestamp, or that file's modification
time, must establish it is not newer than the selected job. If the age is unknown, pass an explicit
ledger path or job ID instead. ID-prefix selection is unchanged.

Results are JSON on stdout; progress is on stderr. `launch --detach` prints one JSON object on one
line, including `job_id` and `ledger`, after the worker is armed and released. Code 9 means handed
off, even if image preparation or GPU scheduling has not finished. Use the returned path for
parallel jobs; `latest` may refer to another concurrent launch. A job that has already ended returns
its outcome instead. `wait` returns the recorded job outcome. Ctrl+C stops waiting only; Ctrl+C on
launch requests cancellation. See [outcomes and recovery](OPERATIONS.md).

`podvigil-modal --version` prints the package version and commit without credentials. Each launch
records its version, commit, modified-source flag and source fingerprint; status keeps that original
identity even when read by a different installation. Unstamped source archives report unknown
commit.

`plan`, `status`, `wait`, `list` and `logs` are offline observers. `logs` shows a bounded cached
tail with its timestamp and age, not a live provider stream. The worker refreshes it about every ten
seconds while running and from the verified final backup. Missing snapshots are reported plainly;
complete stdout/stderr remain in the backup. `--lines` accepts 1–1000. Logs are workload text and
can contain anything the workload prints.

`no_capacity` (4) means recorded evidence proves the workload never started. Any inputs-only Volume
that was created has been verified, backed up and deleted; a start can also expire before either
resource is requested. The outcome is never inferred from an SDK error class alone. Missing
start-protocol evidence retains storage; unknown resource creation remains ambiguous.

Before its deadline, a running Sandbox is left alone by `recover` unless you pass `--stop-running`
or have already requested cancellation. Recovery never resumes monitoring or creates a new job. Use
`--sandbox-id ID` only to reconcile a lost receipt: both ownership tags must match the ledger. It
cannot override an existing, different receipt or declare an unresolved create absent.

`--assert-no-sandbox` is a separate, explicit operator statement for unknown compute without an ID.
It requires a fresh owned lookup miss and time past the fixed deadline plus run timeout. It records
assumed absence, not observed termination; a pending Sandbox could still start late. Verified backup
remains mandatory and completed closure returns `error` (1), never success. It cannot combine with
`--sandbox-id` or `--stop-running`. Read [the recovery rules](OPERATIONS.md#recovery) first.

Cancellation signals the supervisor and gives it up to `finalize_seconds` within the original
deadline to publish results. Hard termination remains the fallback. Publication is best effort.

`recover --accept-partial` can also preserve a damaged root-level `supervisor-error.json` as opaque
bytes. The 64 KiB limit, byte verification and all manifest checks remain. Status reports a
controlled unreadable notice without showing the diagnostic's invalid contents; outcome rules still
follow the manifest when one exists.

The [configuration fields](MODAL.md#job-fields) and
[smoke example](../examples/modal-smoke/modal-job.json) define the job format. The trusted workload
writes checkpoints and final artifacts to `PODVIGIL_OUTPUT_DIR` and reads explicit uploaded files
from `PODVIGIL_INPUT_DIR`. It must finish before the reserved finalization interval.

An optional `startup_timeout_seconds` separates staging, image preparation and scheduling from the
run allowance. With it, `timeout_seconds` is the container run budget, including finalization; the
overall deadline is fixed at launch to the sum of both allowances. Early startup never adds unused
startup time to the run budget. Supervisor initialization and its start-gate commit consume part of
the run allowance. Omit the new field to retain the legacy overall allowance.

New jobs use the start protocol in either mode: if the supervisor enters after its start deadline
(the overall deadline when no startup field is supplied), it exits before writing to the Volume.
Recovery still requires terminal observations and a verified inputs-only backup for `no_capacity`.

If status shows `runtime_start_error`, the worker could not read the start marker. It keeps
monitoring within the existing deadline instead of stopping healthy work on that failed read.
Cancellation still works, and a confirmed missing marker after the startup allowance ends stops the
job for verified cleanup.

Use a small image with the dependencies actually needed, preferably already cached and pinned by
digest. Increasing startup time does not establish that a GPU is available. See the
[three slow-start outcomes](OPERATIONS.md#slow-start-outcomes); no failure triggers another create.

`plan.price_check` compares your hourly estimate with a dated table and warns when yours is more
than 10% lower. Your estimate and its checked timestamp remain authoritative. Final spend is an
estimate based on observed supervisor time, not an invoice or complete provider billing interval.

The workload receives `PODVIGIL_GPU`, `PODVIGIL_CPU`, `PODVIGIL_MEMORY_MIB` and
`PODVIGIL_SECONDS_AVAILABLE`. Use the latter as the remaining workload time before finalization. CPU
is the requested physical-core allocation; `os.cpu_count()` and affinity can expose a different
number. Set framework thread counts deliberately. GPU identity is sampled via `nvidia-smi`, or
`unknown` when unavailable (`none` for a CPU job without a detected GPU).

Initial GPU discovery may wait up to ten seconds for a cold driver. It leaves a 0.25-second launch
margin within the start deadline and useful run window, and is skipped with less than 0.1 seconds
available after that margin. Later samples retain their one-second timeout.

An optional `workload_env` map supplies non-secret child settings, for example
`{"OMP_NUM_THREADS": "2", "TOKENIZERS_PARALLELISM": "false"}`. Values are saved in plaintext in job
records and uploaded metadata. It is not a secret store. Names must match
`[A-Za-z_][A-Za-z0-9_]{0,127}`; `PODVIGIL_` and `MODAL_` prefixes are reserved regardless of case.
Limits: 128 entries, 8192 UTF-8 bytes per value and 65536 bytes total for names plus values; NUL and
non-string values are refused. These settings do not change the supervisor's environment.

Documented variable names are configuration interfaces, not secret values. The runner does not load
a repository `.env` file. Workload logs, input files, ledger contents and backups are not scrubbed
for arbitrary secrets; follow [the sharing rules](../SECURITY.md#credentials-and-shared-records).
