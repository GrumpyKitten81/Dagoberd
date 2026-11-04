---
description: Run Dagoberd doctor and summarize configuration health
---

Run the local health check and report actionable issues:

!`poetry run dagoberd doctor 2>&1 || true`

Summarize the output for the user. Highlight configuration, database, MCP launcher, and OpenCode integration problems. Do not mutate ledger data.
