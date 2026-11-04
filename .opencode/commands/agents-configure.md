---
description: Regenerate the Dagoberd MCP launcher for the active environment
---

Regenerate the installations-bound MCP launcher and verify the handshake:

!`poetry run dagoberd agents configure && poetry run dagoberd agents doctor 2>&1`

Explain whether Codex, OpenCode, Cursor and Claude Code can start the local stdio MCP. Remind the user to rerun this command after changing virtual environments.
