"""Keep test CLI calls away from the developer's registered financial workspaces."""

import pytest


@pytest.fixture(autouse=True)
def isolated_workspace_environment(tmp_path, monkeypatch):
    for name in (
        "DAGOBERD_CONFIG",
        "DAGOBERD_DB",
        "DAGOBERD_WORKSPACE",
        "DAGOBERD_DATA_ROOT",
        "DAGOBERD_CLOUD_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DAGOBERD_WORKSPACE_REGISTRY", str(tmp_path / "registry.toml"))
