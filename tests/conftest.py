"""Keep test CLI calls away from the developer's registered financial workspaces."""

import pytest


@pytest.fixture(autouse=True)
def isolated_workspace_environment(tmp_path, monkeypatch):
    for name in (
        "ACCOUNT_EVAL_CONFIG",
        "ACCOUNT_EVAL_DB",
        "ACCOUNT_EVAL_WORKSPACE",
        "ACCOUNT_EVAL_DATA_ROOT",
        "ACCOUNT_EVAL_CLOUD_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACCOUNT_EVAL_WORKSPACE_REGISTRY", str(tmp_path / "registry.toml"))
