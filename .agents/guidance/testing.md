# Testing guidance


- Add or update tests when the request requires behavioral verification or when a changed behavior lacks relevant regression coverage.
- Use readable `pytest` tests in `tests/` with focused fixtures and meaningful edge or error cases.
- Do not add boilerplate tests, mandatory logging infrastructure, coverage targets, or mocks without a task-specific need.
- Run the narrowest relevant test first, then the full suite when the changed behavior and environment permit it.
- Never verify code changes against real financial data; use temporary synthetic workspaces without private local overrides.
