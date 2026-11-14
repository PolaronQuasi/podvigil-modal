# Contributing

Open issues and pull requests in [PodVigil Modal](https://github.com/PolaronQuasi/podvigil-modal).
The upstream PodVigil repository is a separate project; send Modal runner changes here.

Read [the lifecycle contract](CONTRACT.md) and [security policy](SECURITY.md). Keep credentials,
ledgers, private paths and outputs out of Git. Do not replace source underneath a running worker.

Tests must use fake providers and temporary local state. No CI or development command may allocate
cloud resources. Add regressions for lifecycle failures and report platform skips. Preserve the
existing outcome codes and saved-ledger compatibility.

Run the checks in [Development](docs/DEVELOPMENT.md), update the manual and operations guide for
behavior changes, and keep diagrams consistent with their Mermaid sources.
