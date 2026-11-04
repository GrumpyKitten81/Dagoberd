# Python guidance


- Use Python 3.12 or later and Poetry for dependency management and execution.
- Run project commands through `poetry run`; do not add `requirements.txt`.
- Use PEP 8 names, four-space indentation, absolute imports, and `snake_case` module names.
- Add type annotations to new or changed function signatures where practical; use modern union syntax such as `T | None` when editing Python 3.12 code.
- Use Pydantic for structured models and configuration, `pathlib` for new filesystem path handling, and `python-dotenv` for environment-backed configuration.
- Use `pytest` for tests. Do not introduce formatting, linting, coverage, mocking, logging, or CI tools unless the task explicitly requires them.
- Protect secrets with environment variables and do not commit `.env` files.
