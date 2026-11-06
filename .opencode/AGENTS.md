# OpenCode harness

OpenCode-specific configuration and workflows for Dagoberd.

## Project configuration

- Root config: `opencode.json` (MCP, permissions, skills, agent overrides)
- Subagents: `.opencode/agents/`
- Commands: `.opencode/commands/`

Configure the optional MCP launcher once per active virtual environment:

```bash
poetry run dagoberd agents configure
poetry run dagoberd agents doctor
```

The launcher lives under `.dagoberd/mcp` (gitignored). MCP failure must not block CLI development or an OpenCode session.

## MCP integration

`opencode.json` starts the local stdio MCP through `./Scripts/dagoberd-mcp.sh`. That wrapper prefers the generated `.dagoberd/mcp` launcher and falls back to `poetry run python -m dagoberd.mcp_server` when the launcher is missing.

Sensitive actions remain CLI-only: Comdirect 2FA, secret entry, SMTP send, restore, and delete-run.

## Skills

OpenCode discovers `.agents/skills/dagoberd/` automatically. Activate skill `dagoberd` before financial data workflows. The skill defines the safe 17-step workflow, preview requirements, and security boundaries.

For CLI entry points and MCP tool names, see `.agents/skills/dagoberd/references/interfaces.md`.

## Read-only subagents

Launch these through the `subagent` tool instead of mixing exploration or review into the primary task:

| Subagent | File | Purpose |
| -------- | ---- | ------- |
| `dagoberd-explorer` | `.opencode/agents/dagoberd-explorer.md` | Trace execution paths, data flow, and SQLite effects without modifying files (prompt: `.agents/prompts/dagoberd-explorer.md`) |
| `dagoberd-reviewer` | `.opencode/agents/dagoberd-reviewer.md` | Review correctness, data integrity, security, and missing tests (prompt: `.agents/prompts/dagoberd-reviewer.md`) |

## Slash commands

| Command | Purpose |
| ------- | ------- |
| `/doctor` | Run `dagoberd doctor` and summarize configuration health |
| `/agents-configure` | Regenerate the MCP launcher for the active environment |
| `/handoff` | Run local development gates before handing off code changes |
| `/finance` | Start an Dagoberd financial workflow from a natural-language request |

## Permissions

`opencode.json` defines global `permission` rules: skill access, guarded reads of local secrets, and cautious git push behavior. Subagents add their own edit and shell denials.

## OpenCode precedence

Shared guidance files listed in the root `AGENTS.md` apply to all agents. This file adds OpenCode-specific MCP, skills, subagents, commands, and permissions only.
