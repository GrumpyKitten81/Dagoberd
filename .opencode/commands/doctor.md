---
description: Run AccountEval doctor and summarize configuration health
---

Run the local health check and report actionable issues:

!`poetry run account-eval doctor 2>&1 || true`

Summarize the output for the user. Highlight configuration, database, MCP launcher, and OpenCode integration problems. Do not mutate ledger data.
