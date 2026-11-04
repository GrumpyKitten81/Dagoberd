# Dagoberd agent guidance

Shared guidance for all coding agents in this repository.

## Harness supplements

Read your client-specific supplement at session start:

- **Codex**: `.codex/AGENTS.md`
- **OpenCode**: `.opencode/AGENTS.md`
- **Cursor**: `.cursor/AGENTS.md`
- **Claude Code**: `.claude/AGENTS.md`

OpenCode also loads every `AGENTS.md` on the path from the active location up to the project root. Harness supplements outside that path must be read explicitly when instructed above.

## Shared coding guidance

Read these files before editing code. Apply them in order; later files refine earlier guidance. Do not duplicate global agent rules from user-home Claude or Cursor configuration in repository guidance.

1. `.agents/guidance/general.md`
2. `.agents/guidance/code.md`
3. `.agents/guidance/python.md` for Python changes
4. `.agents/guidance/testing.md` for test changes or verification
5. `.agents/guidance/markdown.md` for Markdown changes

## Dagoberd data workflows

Use `.agents/skills/dagoberd/` for requested Dagoberd data-processing workflows (imports, sync, classification, receipts, reports). Load the skill before mutating ledger data.

Ledger data, original imports, receipts and reports are sensitive personal data. Never commit them to the public code repository. Mutating workflows require an explicit request and the applicable preview and confirmation.

Read `AGENTS.override.md` if present after this guidance; it may add private workspace conventions.

The user starts and configures their coding agent outside Dagoberd. The optional OpenAI-compatible classifier uses separate local model and credential settings and has no research tools. See `doc/AGENT_LLM_INTEGRATION.md` for the division of work and the limits of Dagoberd's privacy gates.

## Development gates

- Require Python 3.12+ and Poetry 2.2+; synchronize the tracked lock file with `poetry sync`.
- Install the Git hook once with `poetry run pre-commit install`.
- Run tests through the Poetry interpreter: `poetry run python -m pytest`.
- Before handing off code, run `poetry run pre-commit run --all-files`, `poetry run python -m pytest`, `poetry check`, and `poetry build`.
- GitHub Actions validates built release artifacts across supported operating systems and runs release security checks; see `doc/GITHUB_ACTIONS.md`. The `quality` workflow also runs formatting, linting, typing, the complete test suite, package validation and builds on every PR and main push.

## Data and local extensions

- Configure an optional private data root, workspace and report extensions locally. Without configuration the application uses its own `output/` directory.
- Personal rules, scripts, skills and report modules belong in a private partner repository; the core provides generic interfaces.
- Never commit local configuration, secrets, real imports, generated reports or machine-specific paths to this repository.
- Close database write connections before versioning a private ledger. Committing and pushing private data follows the user's explicit workspace policy.

## Public issue creation

Use the matching bug, feature or security form in `.github/ISSUE_TEMPLATE/` for every new GitHub issue. For CLI/API creation, copy all required fields into corresponding body sections and include the completed privacy checklist; those clients do not automatically apply issue forms. All issues are public, including security reports. Never attach real financial data or secrets.

## Project language

Maintain project text in English. GUI translations belong in the central English, German and Spanish catalogs; English is the first-use default. Preserve external import headers, legal originals and user-owned data.
