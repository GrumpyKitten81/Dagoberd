---
description: Run local development gates before handing off code changes
agent: build
---

Development gates are defined in root `AGENTS.md`. Run them in order. Stop on the first failure and report the failing command output:

1. `poetry run pre-commit run --all-files`
2. `poetry run python -m pytest`
3. `poetry check`
4. `poetry build`

Summarize pass/fail status for each step. Do not commit unless the user explicitly asks.
