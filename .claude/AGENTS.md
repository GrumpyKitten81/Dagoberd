# Claude Code harness

Claude-specific notes for Dagoberd development.

## Configuration

Claude Code loads the root `AGENTS.md` and your user-home `CLAUDE.md` (AI-DO-NOT and global discipline). Do not duplicate those global rules in repository files.

Use the Dagoberd CLI directly or connect MCP through your Claude Code MCP settings when needed.

Generate the optional stdio launcher for MCP clients:

```bash
poetry run dagoberd agents configure
poetry run dagoberd agents doctor
```

## Subagents and review

Use Claude subagents or delegated tasks for specialized work when available:

- Read-only codebase tracing (exploration without edits)
- Defect-first review of local changes
- Security-focused review of local changes

For Dagoberd explorer and reviewer behavior, read and follow:

- `.agents/prompts/dagoberd-explorer.md`
- `.agents/prompts/dagoberd-reviewer.md`

For financial data workflows, load `.agents/skills/dagoberd/SKILL.md`.

## Claude precedence

Shared guidance under `.agents/guidance/` and policy in the root `AGENTS.md` apply to all agents. This file adds Claude-specific MCP and subagent notes only.
