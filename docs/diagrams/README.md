# Diagrams

[Modal lifecycle](05-modal-lifecycle.svg) is generated from
[its Mermaid source](05-modal-lifecycle.mmd).

The diagram shows managed execution, ordinary recovery and the proven no-workload path. A timeout
alone does not qualify for `no_capacity`. Failures before a Sandbox request use recorded proof that
no create was sent; the separate `--assert-no-sandbox` operator procedure is described in
[Operations](../OPERATIONS.md#recovery), not represented as observed terminal compute here.

The local ledger and any backup remain private. Public diagrams contain configuration concepts, not
actual tokens, job IDs, environment values or machine paths.

Install the pinned Node tools with `npm ci --ignore-scripts`. Because this skips install scripts,
install the headless browser separately when it is not already cached:

```sh
npm exec -- puppeteer browsers install chrome-headless-shell
npm run docs:diagrams
```

Alternatively, set `PUPPETEER_EXECUTABLE_PATH` locally to an installed compatible Chromium browser.
Do not commit that machine-specific path. Rendering uses a temporary headless browser profile, not
your interactive browser session.

Inspect rendered labels and connectors before committing. For a local PNG preview, run
`node tools/render-diagrams.mjs --png`.
