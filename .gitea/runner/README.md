# Gitea runners

The workflow expects an `ubuntu-latest` Linux runner and a `windows` Windows host runner. Both need
Python tooling and Git. The Linux quality job installs pinned build tools. The Windows process tests
require PowerShell's CIM commands for independent worker creation.

Keep runner registration, workspaces and secrets outside the checkout. Do not supply Modal tokens to
any CI job. Tests install dummy credential configuration before invoking SDK contract checks. No
workflow creates a Sandbox, a Volume, a deployed App or a release.

This optional workflow supports Gitea mirrors. Separate `.github/workflows` files run Linux, Windows
and macOS checks in the primary GitHub repository; their jobs are guarded by the GitHub server URL.
They do not register runners or replace this Gitea setup. Local test results do not prove that
hosted runner jobs ran.
