import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from account_eval.cli import main
from account_eval.config import (
    DEFAULT_CONFIG_TOML,
    initialize_project_config,
    load_config,
)
from account_eval.secrets import SecretStore


def _toml_path(path: Path) -> str:
    return path.as_posix()


def test_project_local_and_cli_configuration_precedence(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[database]
path = "project.db"
[privacy]
max_text_length = 200
""",
        encoding="utf-8",
    )
    (tmp_path / "account-eval.local.toml").write_text(
        """[database]
path = "local.db"
""",
        encoding="utf-8",
    )

    loaded = load_config(
        project,
        overrides={"database": {"path": "cli.db"}},
    )

    assert loaded.config.database.path == (tmp_path / "cli.db").resolve()
    assert loaded.config.privacy.max_text_length == 200


def test_cloud_llm_requires_explicit_opt_in(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[llm]
enabled = true
mode = "cloud"
model = "cloud-model"
allow_cloud = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="allow_cloud"):
        load_config(project)


def test_init_creates_configuration_and_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["init"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert Path(output["configuration"]).is_file()
    assert Path(output["database"]).is_file()
    assert (tmp_path / "account-eval.toml").read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML
    assert main(["doctor"]) == 0


def test_cli_registers_and_names_comdirect_and_file_import_accounts(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "accounts",
                "register",
                "comdirect",
                "Comdirect",
                "123456789",
                "Comdirect Giro",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "accounts",
                "register",
                "file",
                "VisaPrivat",
                "VISA-1234",
                "Visa Privat",
                "--type",
                "credit_card",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["accounts", "list"]) == 0
    accounts = json.loads(capsys.readouterr().out)
    assert {(account["provider_kind"], account["display_name"]) for account in accounts} == {
        ("comdirect", "Comdirect Giro"),
        ("file", "Visa Privat"),
    }
    file_account = next(account for account in accounts if account["provider_kind"] == "file")
    assert file_account["account_type"] == "credit_card"
    assert {account["external_id"] for account in accounts} == {
        "…6789",
        "…1234",
    }


def test_local_config_can_use_absolute_data_workspace_paths(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        DEFAULT_CONFIG_TOML
        + """
[comdirect.connections.private]
enabled = true
""",
        encoding="utf-8",
    )
    workspace = tmp_path / "AccountEval"
    (tmp_path / "account-eval.local.toml").write_text(
        f'''[database]
path = "{_toml_path(workspace / "data" / "ledger" / "account_eval.db")}"

[reporting]
output_dir = "{_toml_path(workspace / "data" / "reports")}"

[documents]
storage_dir = "{_toml_path(workspace / "data" / "documents")}"
''',
        encoding="utf-8",
    )

    loaded = load_config(project)

    assert loaded.config.database.path == workspace / "data" / "ledger" / "account_eval.db"
    assert loaded.config.reporting.output_dir == workspace / "data" / "reports"
    assert loaded.config.documents.storage_dir == workspace / "data" / "documents"


def test_data_root_and_cloud_workspace_derive_default_paths(tmp_path):
    project_dir = tmp_path / "AccountEval"
    data_root = tmp_path / "AccountEvalData"
    cloud_workspace = tmp_path / "CloudSync" / "AccountEval"
    project_dir.mkdir()
    data_root.mkdir()
    for name in ("documents", "reports", "imports"):
        (cloud_workspace / name).mkdir(parents=True)
    project = project_dir / "account-eval.toml"
    project.write_text(
        f"""[data]
root = "{_toml_path(data_root)}"

[cloud]
workspace = "{_toml_path(cloud_workspace)}"
""",
        encoding="utf-8",
    )

    loaded = load_config(project)

    assert loaded.config.data.root == data_root.resolve()
    assert loaded.config.cloud.workspace == cloud_workspace.resolve()
    assert loaded.config.database.path == data_root / "ledger" / "account_eval.db"
    assert loaded.config.documents.storage_dir == cloud_workspace / "documents"
    assert loaded.config.reporting.output_dir == cloud_workspace / "reports"


def test_data_root_env_override_and_explicit_paths_win(tmp_path, monkeypatch):
    project_dir = tmp_path / "AccountEval"
    configured_root = tmp_path / "configured-data"
    env_root = tmp_path / "env-data"
    explicit_db = tmp_path / "custom.db"
    project_dir.mkdir()
    configured_root.mkdir()
    env_root.mkdir()
    project = project_dir / "account-eval.toml"
    project.write_text(
        f"""[data]
root = "{_toml_path(configured_root)}"

[database]
path = "{_toml_path(explicit_db)}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ACCOUNT_EVAL_DATA_ROOT", str(env_root))

    loaded = load_config(project)

    assert loaded.config.data.root == env_root.resolve()
    assert loaded.config.database.path == explicit_db.resolve()


def test_unconfigured_data_root_never_selects_a_sibling_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNT_EVAL_DATA_ROOT", raising=False)
    project_dir = tmp_path / "AccountEval"
    data_root = tmp_path / "AccountEvalData"
    project_dir.mkdir()
    data_root.mkdir()
    project = project_dir / "account-eval.toml"
    project.write_text(
        """[database]
path = "output/account_eval.db"
""",
        encoding="utf-8",
    )

    loaded = load_config(project)

    assert loaded.config.data.root is None
    assert loaded.config.database.path == project_dir / "output" / "account_eval.db"


def test_config_show_never_reads_or_prints_secret_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        DEFAULT_CONFIG_TOML
        + """
[comdirect.connections.private]
enabled = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMDIRECT_PASSWORD", "must-not-appear")

    assert main(["--config", str(project), "config", "show", "--masked"]) == 0

    output = capsys.readouterr().out
    assert "must-not-appear" not in output
    assert "comdirect.private.username" in output


def test_config_show_masks_delivery_recipients(tmp_path, capsys):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        DEFAULT_CONFIG_TOML
        + """
[delivery.targets.personal]
recipients = ["owner@example.org"]
""",
        encoding="utf-8",
    )

    assert main(["--config", str(project), "config", "show", "--masked"]) == 0

    output = capsys.readouterr().out
    assert "owner@example.org" not in output
    assert "***@***" in output


def test_legacy_comdirect_configuration_is_rejected(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[comdirect]
enabled = false
password_secret = "COMDIRECT_PASSWORD"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"legacy \[comdirect\]"):
        load_config(project)


def test_named_comdirect_connections_have_isolated_default_keychain_references(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[comdirect.connections.private]
enabled = true

[comdirect.connections.shared]
enabled = true
session_mode = "keychain_timed"
""",
        encoding="utf-8",
    )

    connections = load_config(project).config.comdirect.connections

    assert connections["private"].username_secret == "comdirect.private.username"
    assert connections["shared"].client_id_secret == "comdirect.shared.client-id"
    assert connections["private"].client_secret_secret != connections["shared"].client_secret_secret


def test_named_comdirect_connections_cannot_override_credential_references(tmp_path):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[comdirect.connections.private]
username_secret = "shared.username"

[comdirect.connections.shared]
username_secret = "shared.username"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"must be 'comdirect\.private\.username'"):
        load_config(project)


def test_default_example_matches_generated_configuration(tmp_path):
    target = initialize_project_config(tmp_path / "account-eval.toml")
    repository_example = Path(__file__).parents[2] / "account-eval.toml.example"

    assert target.read_text(encoding="utf-8") == repository_example.read_text(encoding="utf-8")


def test_secret_store_prefers_keyring_over_environment(monkeypatch):
    import keyring

    monkeypatch.setenv("COMDIRECT_USERNAME", "environment")
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, name: "keyring",
    )

    assert SecretStore().get("comdirect.username") == "keyring"


def test_secret_store_uses_environment_after_keyring(monkeypatch):
    import keyring

    monkeypatch.setenv("COMDIRECT_USERNAME", "environment")
    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)

    assert SecretStore().get("comdirect.username") == "environment"


def test_secret_store_uses_dotenv_only_as_last_fallback(tmp_path, monkeypatch):
    import keyring

    name = "ACCOUNT_EVAL_TEST_DOTENV_SECRET"
    monkeypatch.setattr(keyring, "get_password", lambda service, key: None)
    monkeypatch.delenv(name, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"{name}=dotenv\n", encoding="utf-8")
    try:
        assert SecretStore(dotenv_path=dotenv_path).get(name) == "dotenv"
    finally:
        os.environ.pop(name, None)


def test_set_secret_writes_only_to_keyring(monkeypatch):
    import keyring

    stored = {}
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, name, value: stored.update(
            service=service,
            name=name,
            value=value,
        ),
    )

    SecretStore().set("comdirect.client-secret", "sensitive")

    assert stored == {
        "service": "account-eval",
        "name": "COMDIRECT_CLIENT_SECRET",
        "value": "sensitive",
    }


def test_cli_secret_input_is_prompted_and_not_passed_as_argument(
    tmp_path,
    monkeypatch,
    capsys,
):
    import keyring

    project = tmp_path / "account-eval.toml"
    project.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    stored = {}
    monkeypatch.setattr(
        "account_eval.cli.getpass.getpass",
        lambda prompt: "prompted-secret",
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, name, value: stored.update(
            service=service,
            name=name,
            value=value,
        ),
    )

    assert (
        main(
            [
                "--config",
                str(project),
                "config",
                "set-secret",
                "comdirect.private.client-secret",
            ]
        )
        == 0
    )

    assert stored["value"] == "prompted-secret"
    assert "prompted-secret" not in capsys.readouterr().out
