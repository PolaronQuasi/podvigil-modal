# Development

This is a Modal-only project. Local checks and CI never contact Modal or create paid resources. Use
a separate checkout if a worker is still using source files.

## Environments

Create a virtual environment and install `requirements.lock.txt` for the pinned SDK and its
dependencies. A clean dependency-free environment also runs all tests except the SDK contract test.
Install `requirements-ci.txt` in a separate CI environment for formatting, lint and packaging.

```sh
python -B tools/ci.py test
python -m ruff check --select E9,F63,F7,F82 src tests tools examples
python tools/format_files.py --check --python-only
python tools/ci.py audit
python tools/check_docs.py
python tools/check_workflows.py
python -m build --no-isolation
python -m twine check --strict dist/*.whl dist/*.tar.gz
python -B tools/ci.py smoke
```

Use the appropriate environment's Python executable. Test reports and source hashes are written
under ignored `output/ci`. The suite replaces Modal credentials with dummy values and isolated
state. Skips identify platform-specific coverage; report them rather than treating them as passes.
The publication audit checks tracked and nonignored files for private file classes and common
credential formats; it never prints matched values. It cannot recognize every possible secret.
Before sharing reports or changing history, review
[credentials and shared records](../SECURITY.md#credentials-and-shared-records).

Tests cover fake cloud lifecycle failures, scheduling cancellation, ownership checks, complete
backup verification, directory durability, lock contention, interrupted handoff, outcome recovery,
read-only observers and stable job selection. Process tests use the real detached launcher with
dummy credential proofs. Linux tests run the actual Sandbox entrypoint without cloud access. Review
regressions also cover transient polling outages, loop stalls, bounded cancellation, inputs-only
cleanup, encoded output paths, repeated backup reuse, nonfatal periodic commits and symbolic-link
metadata. The pinned SDK lane binds lookup, streaming-file and signal-execution seams without
contacting Modal. See [Modal verification](MODAL.md#verification-and-sources) for the remaining
live-test gaps.

The failure matrix injects a failure before and after every lifecycle-record write and at every
provider-call boundary in a successful fake job. Each case checks that creation is never repeated
and remote deletion has matching durable backup evidence. A real Linux process test sends SIGTERM to
the supervisor, verifies the workload's cancellation checkpoint and manifest, and checks that a
child in a separate session is reaped. Recovery also tests a saved cancellation left by a dead
worker.

For a local Linux run, use an already-installed Python image with `--pull=never --network none` and
mount only source/test/tool inputs, never credentials or a Docker socket.

## Package checks

The wheel is `podvigil_modal-0.1.0-py3-none-any.whl`; the sole console entry point is
`podvigil-modal`. Package smoke installs it without dependencies in a fresh environment, runs the
CLI outside the checkout and proves offline planning/status work without an SDK. Inspection checks
for leaked private files and unexpected cloud-provider modules or dependencies.

Build fresh distributions after source changes. Neither building nor testing publishes a release.
The version stays in `src/podvigil/__init__.py`.

## Optional Gitea CI

[The workflow](../.gitea/workflows/ci.yml) runs Linux SDK tests, Windows tests, lint, documentation,
credential scans and package verification. A Linux job also exercises Python 3.10 without the SDK.
Its quality job also lints the GitHub workflows. CI receives no provider credentials. See
[runner setup](../.gitea/runner/README.md).

## GitHub CI

[CI](../.github/workflows/ci.yml) calls the reusable
[verification workflow](../.github/workflows/verify.yml), following upstream PodVigil's structure.
CI runs for pushes to `main`, pull requests and manual dispatches using GitHub-hosted runners. Every
GitHub job checks the server URL; Gitea mirrors can use the separate workflow above. No self-hosted
runner registration is required for GitHub. The verification jobs cover:

- Ubuntu 24.04: Python 3.10 without dependencies, and 3.10, 3.12 and 3.14 with the pinned SDK.
- Windows Server 2025: Python 3.12 and the real detached-worker tests, using the runner's temporary
  directory.
- macOS 26 on Apple Silicon and Intel: Python 3.12, portable tests and detached-worker checks.
  Linux-only supervisor checks remain explicitly skipped on macOS and Windows.
- Linux containers: the complete dependency-free suite with and without Docker's init, using the
  pinned Python image and `--network none`. Only a Git archive enters the container; no checkout,
  credentials or Docker socket is mounted.
- Quality and distributions: lint, formatting, credential and documentation audits, workflow
  validation, wheel/sdist build and installed CLI smoke tests. Both Mac architectures then verify
  checksums and test those same packages without rebuilding them.

`All checks passed` requires every verification job to succeed, including the two Mac matrices.
Actions are pinned by commit, permissions are read-only, jobs have timeouts, and logs and package
artifacts are retained for 14 days. No workflow publishes a release or contacts Modal. Dependency,
image and lint-tool downloads happen before offline tests; no provider credentials are needed.

`python tools/check_workflows.py` downloads the pinned actionlint archive for the host architecture,
verifies its SHA-256 and checks workflow syntax and expressions. It requires network access to
GitHub release assets. Local Windows/Linux results and syntax checks do not establish that the
hosted Mac jobs have run; check the repository's Actions page for hosted results.

Installed `--version` and ledger identity include the source commit when built from Git. Build hooks
stamp wheel/sdist metadata without modifying source. A source-only staging copy must retain a
generated `src/podvigil/_build_info.json` from the source identity before packaging, or report its
commit as unknown. Never hand-edit a commit claim. Package smoke checks the stamp against the source
identity and verifies the installed command outside the checkout without an SDK or credentials.

Use `npm ci --ignore-scripts` for pinned documentation tooling. Format maintained files with
`python tools/format_files.py --write`; stage new files first so the inventory includes them. Edit
Mermaid sources and render with `npm run docs:diagrams`; inspect the output visually.
