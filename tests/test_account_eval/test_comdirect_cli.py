import json
from datetime import UTC, datetime

from account_eval.cli import _store_timed_comdirect_session, main
from account_eval.config import DEFAULT_CONFIG_TOML, load_config
from account_eval.providers.comdirect import ComdirectSessionState


def test_auth_command_stores_timed_session_without_printing_tokens(
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
session_mode = "keychain_timed"
""",
        encoding="utf-8",
    )
    state = ComdirectSessionState(
        access_token="secret-access-token",
        session_id="session",
        refresh_token="secret-refresh-token",
        expires_at=None,
        authenticated_at=datetime.now(UTC).isoformat(),
    )
    monkeypatch.setattr(
        "account_eval.cli._authenticate_comdirect",
        lambda *args: state,
    )
    stored = {}
    monkeypatch.setattr(
        "account_eval.cli.SecretStore.set",
        lambda self, name, value: stored.update(name=name, value=value),
    )

    assert (
        main(
            [
                "--config",
                str(project),
                "auth",
                "comdirect",
                "private",
            ]
        )
        == 0
    )

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["authenticated"] is True
    assert stored["name"] == "comdirect.private.timed-session"
    assert "secret-access-token" in stored["value"]
    assert "secret-access-token" not in output_text
    assert "secret-refresh-token" not in output_text


def test_timed_sessions_use_separate_keychain_entries_per_connection(tmp_path, monkeypatch):
    project = tmp_path / "account-eval.toml"
    project.write_text(
        """[comdirect.connections.private]
enabled = true
session_mode = "keychain_timed"

[comdirect.connections.shared]
enabled = true
session_mode = "keychain_timed"
""",
        encoding="utf-8",
    )
    state = ComdirectSessionState(
        access_token="access",
        session_id="session",
        refresh_token="refresh",
        expires_at=None,
        authenticated_at=datetime.now(UTC).isoformat(),
    )
    stored: list[str] = []
    monkeypatch.setattr(
        "account_eval.cli.SecretStore.set",
        lambda self, name, value: stored.append(name),
    )
    config = load_config(project).config.comdirect

    _store_timed_comdirect_session("private", config.connection("private"), state)
    _store_timed_comdirect_session("shared", config.connection("shared"), state)

    assert stored == ["comdirect.private.timed-session", "comdirect.shared.timed-session"]
