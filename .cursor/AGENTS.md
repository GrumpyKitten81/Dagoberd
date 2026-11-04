# Cursor harness

Cursor-specific notes for AccountEval development.

## Configuration

Cursor reads the root `AGENTS.md` automatically. No separate MCP project file is required; use the AccountEval CLI directly or configure MCP through your Cursor MCP settings if needed.

Generate the optional stdio launcher for MCP clients:

```bash
poetry run account-eval agents configure
poetry run account-eval agents doctor
```

## Subagents and review

Use Cursor subagents for specialized work when available:

- **Explore** or **generalPurpose** for read-only codebase tracing
- **bugbot** for defect-first review of local changes
- **security-review** for security-focused review of local changes

For AccountEval explorer and reviewer behavior, read and follow:

- `.agents/prompts/account-eval-explorer.md`
- `.agents/prompts/account-eval-reviewer.md`

For AccountEval financial data workflows, follow `.agents/skills/account-eval/SKILL.md`.

## Cursor precedence

Shared guidance files listed in the root `AGENTS.md` apply to all agents. This file adds Cursor-specific subagent and MCP notes only.
