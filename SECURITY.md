# Security

Do not include tokens, private job ledgers or workload data in public issues.

Use this repository's [security page](https://github.com/PolaronQuasi/podvigil-modal/security) to
report a vulnerability privately when GitHub offers that option. If private reporting is
unavailable, open an issue asking the maintainer to arrange a private reporting channel, without
including vulnerability details. No email address is required. Do not report Modal runner
vulnerabilities to the separate upstream project.

Modal tokens belong in the SDK's local profile. The runner does not upload account tokens, SDK
Secrets or OIDC identity tokens to the Sandbox. Use trusted images and workloads: the workload can
access its mounted Volume and runtime files.

## Credentials and shared records

Documenting environment-variable names is safe; publishing their private values is not. This tool
does not load a repository `.env` file. Configure Modal through its local SDK profile, outside the
checkout; do not copy tokens into examples, command arguments or `workload_env`.

`workload_env` is deliberately non-secret configuration. Its values are stored in plaintext in the
local ledger and uploaded `run.json`. Explicit input files are uploaded too. Filename checks reject
common credential files but do not inspect arbitrary file contents or detect every renamed secret.

Workload stdout/stderr are not redacted. Cached log tails, checkpoints, downloaded backups and
status documents can include workload data, resource IDs and local paths. The sanitized watchdog
error log does not make those other records safe to publish. Share a minimal, manually reviewed
excerpt when reporting a problem; keep full ledgers and backups private.

`python tools/ci.py audit` rejects private file classes and common credential formats without
printing matched values. Package checks apply the same rules. These checks and `.gitignore` are
defenses against accidental publication, not a guarantee that arbitrary content contains no secrets.
Do not force-add ignored files or post an environment dump.

See [the lifecycle contract](CONTRACT.md) and
[operational limits](docs/MODAL.md#operational-limits).
