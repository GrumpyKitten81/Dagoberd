# Code guidance


- Make the smallest complete change that satisfies the request.
- Use meaningful names, clear interfaces, single sources of truth, and focused functions.
- Prefer explicit validation and focused error handling at external or untrusted boundaries.
- Keep code type-safe and avoid unnecessary duplication, nesting, and magic values when a named domain concept already exists.
- Keep source files below 750 lines when a natural, task-relevant split exists.
- Preserve existing behavior unless the request explicitly changes it. Do not remove unrelated code, valid comments, or functionality.
- Add dependencies only when required by the request and report their exact installation command.

For Claude Code, the user's global `CLAUDE.md` defines AI-DO-NOT rules and takes precedence over broader harness defaults about refactoring, logging, documentation, tests, compatibility, or extensibility. Do not duplicate those global rules in repository guidance. The files under `.agents/guidance/` add Dagoberd-specific constraints only.
