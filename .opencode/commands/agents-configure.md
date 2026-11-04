---
description: Regenerate the AccountEval MCP launcher for the active environment
---

Regenerate the installations-bound MCP launcher and verify the handshake:

!`poetry run account-eval agents configure && poetry run account-eval agents doctor 2>&1`

Explain whether Codex, OpenCode, Cursor and Claude Code can start the local stdio MCP. Remind the user to rerun this command after changing virtual environments.
