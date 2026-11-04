# Claude Code harness

Claude-specific notes for AccountEval development.

## Configuration

Claude Code loads the root `AGENTS.md` and your user-home `CLAUDE.md` (AI-DO-NOT and global discipline). Do not duplicate those global rules in repository files.

Use the AccountEval CLI directly or connect MCP through your Claude Code MCP settings when needed.

Generate the optional stdio launcher for MCP clients:

```bash
poetry run account-eval agents configure
poetry run account-eval agents doctor
```

## Subagents and review

Use Claude subagents or delegated tasks for specialized work when available:

- Read-only codebase tracing (exploration without edits)
- Defect-first review of local changes
- Security-focused review of local changes

For AccountEval explorer and reviewer behavior, read and follow:

- `.agents/prompts/account-eval-explorer.md`
- `.agents/prompts/account-eval-reviewer.md`

For financial data workflows, load `.agents/skills/account-eval/SKILL.md`.

## Claude precedence

Shared guidance under `.agents/guidance/` and policy in the root `AGENTS.md` apply to all agents. This file adds Claude-specific MCP and subagent notes only.
