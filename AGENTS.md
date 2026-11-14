# Modal-only project policy

- This Modal-only project derives from upstream PodVigil commit
  `81577fcafbb65dd7ad089e634918e00735bacdbb`.
- The primary repository is `https://github.com/PolaronQuasi/podvigil-modal`.
- Work only in this project. Do not modify the separate upstream PodVigil repository or checkout.
- The only provider is Modal. Do not add other cloud adapters or their credentials.
- Never run a paid job without explicit user authorization. Local tests and CI are offline.
- Use the managed lifecycle with a finite Sandbox timeout, independent worker and owned Volume.
- Persist creation intent before requesting resources. Never repeat an ambiguous create.
- Only act on receipted resources whose ownership is verified.
- Attempt compute termination even when logging or status observations fail. Never infer termination
  from an accepted request; require independent terminal observations before retrieving outputs.
- Delete storage only after complete inventory, byte verification and durable local backup. Retain
  storage when any required evidence is missing.
- Only the worker holding the ledger lock may change lifecycle records after handoff.
- Credentials remain in Modal's local profile, never source, job inputs, logs or workloads.
- Preserve existing ledgers and results. Use a separate checkout if a worker is running.
- State the trusted-workload limitation and distinguish stopped compute from storage charges.
