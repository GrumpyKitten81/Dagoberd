from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from account_eval.accounts import AccountScope
from account_eval.cli import main
from account_eval.config import ReportExtensionConfig, load_config
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.mcp_server import MCPService
from account_eval.profiles import install_builtin_profiles
from account_eval.reporting.extensions import load_report_extension, validate_report_artifacts
from account_eval.reporting.ledger_reports import LedgerReportGenerator

EXTENSION = Path(__file__).parents[1] / "fixtures/report_extension/__init__.py"


def _repository(tmp_path: Path) -> LedgerRepository:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    repository.import_transactions(
        "file",
        "extension-test",
        [
            NormalizedTransaction(
                "current", date(2026, 7, 1), Decimal("266"), "EUR", "Current", "one"
            ),
            NormalizedTransaction(
                "history", date(2026, 6, 1), Decimal("266"), "EUR", "History", "one"
            ),
            NormalizedTransaction("other", date(2026, 7, 1), Decimal("266"), "EUR", "Other", "two"),
            NormalizedTransaction(
                "currency", date(2026, 7, 1), Decimal("266"), "USD", "Currency", "one"
            ),
        ],
    )
    return repository


@pytest.mark.parametrize("include_history", [False, True])
def test_extension_receives_scoped_data_and_registers_artifacts(tmp_path, include_history):
    repository = _repository(tmp_path)
    rows = {row["purpose"]: row for row in repository.list_transactions(limit=None)}
    before_modules = set(sys.modules)
    generator = LedgerReportGenerator(
        repository,
        tmp_path / "reports",
        extensions={
            "custom": ReportExtensionConfig(
                path=EXTENSION,
                profile="rental",
                include_history=include_history,
            )
        },
    )
    paths = generator.generate(
        extension="custom",
        formats=("json",),
        account_scope=AccountScope.selected((rows["Current"]["account_id"],)),
        booking_from="2026-07-01",
        booking_to="2026-07-31",
        currency="EUR",
    )
    content = json.loads(paths[0].read_text())
    assert content["ids"] == [rows["Current"]["id"]]
    if include_history:
        assert set(content["history_ids"]) == {rows["History"]["id"], rows["Current"]["id"]}
    else:
        assert content["history_ids"] is None
    metadata = json.loads(paths[1].read_text())
    assert metadata["extension"] == {
        "name": "custom",
        "api_version": 1,
        "include_history": include_history,
    }
    assert metadata["currency_filter"] == "EUR"
    assert metadata["transaction_count"] == 1
    with repository.database.connect() as connection:
        run = connection.execute("SELECT * FROM ledger_report_runs").fetchone()
        assert json.loads(run["output_paths_json"]) == [str(path) for path in paths]
        assert run["transaction_count"] == 1
    assert not any(
        name.startswith("_account_eval_report_") for name in set(sys.modules) - before_modules
    )
    second_paths = generator.generate(extension="custom", formats=("json",))
    assert second_paths[0].parent != paths[0].parent


def test_extension_selection_and_failures_do_not_register_runs(tmp_path):
    repository = _repository(tmp_path)
    generator = LedgerReportGenerator(
        repository,
        tmp_path / "reports",
        extensions={"custom": ReportExtensionConfig(path=EXTENSION, profile="rental")},
    )
    with pytest.raises(ValueError, match="Unknown report extension"):
        generator.generate(extension="missing")
    with pytest.raises(ValueError, match="does not match"):
        generator.generate(profile_code="household", extension="custom")
    with pytest.raises(ValueError, match="every requested format"):
        generator.generate(extension="custom", formats=("json", "html"))
    with load_report_extension(EXTENSION) as generate:
        assert callable(generate)
    with (
        pytest.raises(ValueError, match="API_VERSION"),
        load_report_extension(EXTENSION.with_name("unsupported.py")),
    ):
        pytest.fail("Unsupported API must fail before execution")
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger_report_runs").fetchone()[0] == 0


def test_artifact_validation_rejects_missing_outside_and_duplicate_paths(tmp_path):
    output = tmp_path / "report"
    output.mkdir()
    valid = output / "report.json"
    valid.write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    for artifacts in ([], [output / "missing.json"], [outside], [valid, valid]):
        with pytest.raises(ValueError):
            validate_report_artifacts(artifacts, output, ("json",))


def test_artifact_validation_rejects_symlinks_outside_output(
    tmp_path: Path, create_symlink: Callable[[Path, Path], None]
) -> None:
    output = tmp_path / "report"
    output.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = output / "link.json"
    create_symlink(link, outside)
    with pytest.raises(ValueError, match="inside"):
        validate_report_artifacts([link], output, ("json",))


def test_local_config_and_cli_select_extension(tmp_path, capsys):
    repository = _repository(tmp_path)
    project = tmp_path / "account-eval.toml"
    project.write_text('[database]\npath = "ledger.db"\n[reporting]\noutput_dir = "reports"\n')
    (tmp_path / "account-eval.local.toml").write_text(
        f'[reporting.extensions.custom]\npath = "{EXTENSION.as_posix()}"\nprofile = "rental"\n'
    )
    loaded = load_config(project)
    assert loaded.config.reporting.extensions["custom"].path == EXTENSION.resolve()
    assert (
        main(["--config", str(project), "report", "--extension", "custom", "--formats", "json"])
        == 0
    )
    paths = [Path(line) for line in capsys.readouterr().out.splitlines()]
    assert json.loads(paths[-1].read_text())["extension"]["name"] == "custom"
    result = MCPService(config_path=str(project)).execute(
        "generate_report",
        {"extension": "custom", "formats": ["json"], "idempotency_key": "extension-report"},
    )
    assert json.loads(Path(result["paths"][-1]).read_text())["extension"]["name"] == "custom"
    assert len(repository.list_transactions(limit=None)) == 4
