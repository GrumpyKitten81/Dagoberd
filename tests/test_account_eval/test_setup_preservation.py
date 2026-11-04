from __future__ import annotations

import tomllib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from account_eval.config import DEFAULT_CONFIG_TOML, load_config
from account_eval.onboarding import SetupAnswers, configure_local_project


@pytest.mark.parametrize("inline_reporting", [False, True])
def test_repeated_setup_preserves_unmanaged_values(tmp_path, monkeypatch, inline_reporting):
    monkeypatch.delenv("ACCOUNT_EVAL_DATA_ROOT", raising=False)
    monkeypatch.delenv("ACCOUNT_EVAL_CLOUD_WORKSPACE", raising=False)
    project = tmp_path / "account-eval.toml"
    project.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    (tmp_path / "private-data").mkdir()
    (tmp_path / "private-cloud").mkdir()
    reporting = (
        'reporting = {base_currency = "EUR", output_dir = "reports", default_formats = ["json"], '
        'extensions = {custom = {path = "private/report.py", profile = "rental", include_history = true}}}\n'
        if inline_reporting
        else '[reporting]\nbase_currency = "EUR"\noutput_dir = "reports"\ndefault_formats = ["json"]\n'
        '[reporting.extensions."custom"]\npath = "private/report.py"\nprofile = "rental"\ninclude_history = true\n'
    )
    content = (
        reporting
        + """
[data]
root = "private-data"
[cloud]
workspace = "private-cloud"
[comdirect.connections.primary]
enabled = false
session_mode = "keychain_timed"
allow_mcp_sync = true
[comdirect.connections.second]
enabled = true
timeout_seconds = 42
[llm]
enabled = false
mode = "local"
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = ""
allow_cloud = false
max_tokens = 1024
batch_size = 7
[documents]
auto_match_threshold = 0.98
"""
    )
    local = tmp_path / "account-eval.local.toml"
    local.write_text(content, encoding="utf-8")
    expected = tomllib.loads(content)
    for answers in (
        SetupAnswers(),
        SetupAnswers(llm_preset="ollama", llm_model="test-model", base_currency="USD"),
    ):
        configure_local_project(project, answers)
        actual = tomllib.loads(local.read_text(encoding="utf-8"))
        preserved = deepcopy(actual)
        preserved["comdirect"]["connections"]["primary"]["enabled"] = expected["comdirect"][
            "connections"
        ]["primary"]["enabled"]
        preserved["reporting"]["base_currency"] = expected["reporting"]["base_currency"]
        for key in ("enabled", "mode", "provider", "base_url", "model", "allow_cloud"):
            preserved["llm"][key] = expected["llm"][key]
        assert preserved == expected
        loaded = load_config(project)
        assert loaded.config.database.path == tmp_path / "private-data/ledger/account_eval.db"
        assert not (tmp_path / "output/account_eval.db").exists()
    assert actual["llm"]["model"] == "test-model"
    assert actual["reporting"]["base_currency"] == "USD"


@pytest.mark.parametrize(
    "content", ['[reporting]\ndefault_formats = ["invalid"]\n', "[data]\nroot = [\n"]
)
def test_invalid_setup_never_replaces_local_file_or_creates_database(tmp_path, content):
    project = tmp_path / "account-eval.toml"
    project.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    local = tmp_path / "account-eval.local.toml"
    local.write_text(content, encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        configure_local_project(project, SetupAnswers())
    assert local.read_text(encoding="utf-8") == content
    assert not (tmp_path / "output").exists()
