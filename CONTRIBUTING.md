# Contributing

Use Python 3.12+, Poetry 2.2+ and Git. Synchronize the tracked lockfile and install the hook after cloning:

```bash
poetry sync
poetry run pre-commit install
poetry run pre-commit run --all-files
poetry run python -m pytest
poetry check
poetry build
```

On Windows, the full file-safety suite requires symbolic-link privileges (Developer Mode or a suitably privileged runner). Keep these regressions enabled.

Keep changes focused, typed, migration-safe and covered by meaningful tests. Never use real banking, model, SMTP or document data in fixtures, logs or issues. Mutations require audit, idempotency and applicable version checks. External actions require a visible preview and explicit confirmation. Consult the [Runbook](doc/RUNBOOK.md) and [PRD](doc/PRD.md).

## Language

Maintain documentation, CLI output, reports, comments and synthetic examples in English. The GUI supports English, German and Spanish through central catalogs under `src/account_eval/gui/static/locales/`. English is the default and reference catalog. Add matching keys, named placeholders and plural variants to every catalog. A new language also needs a language-registry entry in `i18n.js`. Use `msg`, `date`, `number` and `amount` with the DOM binding helpers; do not embed translated HTML or rerender forms when switching languages.

Preserve external file headers, source-language import fixtures, legal originals and existing user data. Do not translate command names, JSON keys, database identifiers or import mappings.

## Issues and pull requests

Use the appropriate **Bug report**, **Feature request** or **Security report** form in GitHub Issues. All issues, including security reports, are public. Review the privacy checklist before submitting; synthetic examples are preferred.

When creating an issue through a CLI, API or agent, read the matching form in `.github/ISSUE_TEMPLATE/` and reproduce every required field as a body section, followed by the completed privacy checklist. These clients do not automatically apply GitHub issue forms. Do not create an empty issue or bypass the required information. Existing issues do not need retroactive conversion.

Describe the problem, resulting behavior and relevant verification in each pull request. Keep private financial data and local configuration out of patches and attachments. Publishing, history rewriting and repository visibility changes require a separate explicit instruction.
