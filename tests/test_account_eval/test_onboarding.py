from __future__ import annotations

import os
import sys
from pathlib import Path

from account_eval.config import DEFAULT_CONFIG_TOML, load_config
from account_eval.onboarding import (
    SetupAnswers,
    configure_agent_launcher,
    configure_local_project,
    create_demo_workspace,
    diagnose_agent_runtime,
    diagnose_claude_integration,
    diagnose_codex_integration,
    diagnose_opencode_integration,
    run_offline_self_test,
)


def test_setup_writes_only_secure_non_secret_overrides(tmp_path: Path) -> None:
    config = tmp_path / "account-eval.toml"
    result = configure_local_project(
        config,
        SetupAnswers(
            comdirect_enabled=True,
            llm_preset="ollama",
            llm_model="local-test-model",
            base_currency="EUR",
        ),
    )
    loaded = load_config(config)
    assert loaded.config.comdirect.connections["primary"].enabled is True
    assert loaded.config.llm.model == "local-test-model"
    assert result["secrets_written"] is False
    assert "password" not in loaded.local_path.read_text(encoding="utf-8").lower()
    if os.name != "nt":
        assert loaded.local_path.stat().st_mode & 0o777 == 0o600


def test_agent_launcher_uses_active_environment_without_poetry(tmp_path: Path) -> None:
    result = configure_agent_launcher(tmp_path)
    launcher = Path(result["launcher"])
    content = launcher.read_text(encoding="utf-8")
    assert launcher.is_file()
    assert "poetry run" not in content
    assert "py -3" not in content
    assert sys.executable in content
    assert result["required"] is False


def test_agent_doctor_succeeds_after_launcher_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "account-eval.toml"
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    configure_agent_launcher(tmp_path)
    diagnosis = diagnose_agent_runtime(tmp_path, config_path=config_path)
    assert diagnosis["status"] in {"ok", "warning"}
    assert diagnosis["handshake"]["safe_tools_present"] is True
    assert "opencode" in diagnosis
    assert "codex" in diagnosis
    assert "claude" in diagnosis


def test_opencode_integration_files_are_present_in_repository() -> None:
    root = Path(__file__).resolve().parents[2]
    diagnosis = diagnose_opencode_integration(root)
    assert diagnosis["status"] == "ok", diagnosis["issues"]
    assert diagnosis["issues"] == []


def test_claude_integration_files_are_present_in_repository() -> None:
    root = Path(__file__).resolve().parents[2]
    diagnosis = diagnose_claude_integration(root)
    assert diagnosis["status"] == "ok", diagnosis["issues"]
    assert diagnosis["issues"] == []


def test_codex_integration_files_are_present_in_repository() -> None:
    root = Path(__file__).resolve().parents[2]
    diagnosis = diagnose_codex_integration(root)
    assert diagnosis["status"] == "ok", diagnosis["issues"]
    assert diagnosis["issues"] == []


def test_codex_integration_reports_invalid_executor_definition(tmp_path: Path) -> None:
    agents = tmp_path / ".codex" / "agents"
    agents.mkdir(parents=True)
    (tmp_path / ".codex" / "AGENTS.md").write_text("# Codex\n", encoding="utf-8")
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.account-eval]\ncommand = "./Scripts/account-eval-mcp.sh"\n',
        encoding="utf-8",
    )
    for filename, name, sandbox in (
        ("account-eval-explorer.toml", "account_eval_explorer", "read-only"),
        ("account-eval-executor.toml", "account_eval_executor", "read-only"),
        ("account-eval-reviewer.toml", "account_eval_reviewer", "read-only"),
    ):
        (agents / filename).write_text(
            "\n".join(
                (
                    f'name = "{name}"',
                    'description = "test"',
                    f'sandbox_mode = "{sandbox}"',
                    'developer_instructions = "test"',
                )
            ),
            encoding="utf-8",
        )

    diagnosis = diagnose_codex_integration(tmp_path)

    assert diagnosis["status"] == "warning"
    assert "codex_agent_sandbox_mode_mismatch:account-eval-executor.toml" in diagnosis["issues"]


def test_setup_preserves_manual_delivery_targets(tmp_path: Path) -> None:
    config = tmp_path / "account-eval.toml"
    configure_local_project(config, SetupAnswers())
    local = tmp_path / "account-eval.local.toml"
    local.write_text(
        """[delivery]
enabled = true
host = "smtp.example.org"
from_address = "sender@example.org"

[delivery.targets.personal]
recipients = ["owner@example.org"]
""",
        encoding="utf-8",
    )

    configure_local_project(config, SetupAnswers(base_currency="EUR"))

    loaded = load_config(config)
    assert loaded.config.delivery.enabled is True
    assert loaded.config.delivery.targets["personal"].recipients == ("owner@example.org",)


def test_demo_and_offline_self_test_use_only_synthetic_data(tmp_path: Path) -> None:
    demo = create_demo_workspace(tmp_path / "demo")
    assert demo["accounts"] == 3
    assert demo["transactions"] == 5
    assert demo["contains_personal_data"] is False
    result = run_offline_self_test()
    assert result["status"] == "ok"
    assert result["external_network_used"] is False
    assert result["mcp"]["safe_tools_present"] is True
