from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from dagoberd.accounts import AccountScope
from dagoberd.cli import main
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError
from dagoberd.mcp_server import TOOL_DEFINITIONS, MCPService
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService


def _services(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "analysis.db"))
    install_builtin_profiles(repository)
    return repository, OrganizationService(repository), AnalysisService(repository)


def _booking(
    organization: OrganizationService,
    account_id: str,
    key: str,
    amount: str,
    purpose: str,
    booking_date: date,
):
    return organization.create_manual_transaction(
        account_id,
        booking_date=booking_date,
        amount=Decimal(amount),
        purpose=purpose,
        idempotency_key=key,
    )


def test_saved_analysis_rates_cashflow_fixed_costs_trends_and_net_worth(tmp_path):
    repository, organization, analyses = _services(tmp_path)
    eur = organization.create_manual_account(
        "EUR Vermögen",
        account_kind="asset",
        currency="EUR",
        opening_balance=Decimal("1000"),
        balance_date=date(2026, 7, 1),
        idempotency_key="analysis-eur-account",
    )
    usd = organization.create_manual_account(
        "USD Vermögen",
        account_kind="asset",
        currency="USD",
        opening_balance=Decimal("200"),
        balance_date=date(2026, 7, 1),
        idempotency_key="analysis-usd-account",
    )
    debt = organization.create_manual_account(
        "Darlehen",
        account_kind="liability",
        currency="EUR",
        opening_balance=Decimal("300"),
        balance_date=date(2026, 7, 1),
        idempotency_key="analysis-debt-account",
    )
    income = _booking(
        organization,
        eur["id"],
        "analysis-income",
        "1000",
        "Honorar",
        date(2026, 7, 10),
    )
    fixed = _booking(
        organization,
        eur["id"],
        "analysis-fixed",
        "-100",
        "Miete Büro",
        date(2026, 7, 12),
    )
    usd_income = _booking(
        organization,
        usd["id"],
        "analysis-usd-income",
        "100",
        "USD Honorar",
        date(2026, 7, 15),
    )
    organization.create_tag("fixed")
    organization.tag_transaction(fixed["id"], "fixed", expected_version=fixed["version"])
    repository.classify(
        income["id"],
        "freelance",
        "income.freelance",
        "manual",
        1.0,
        "Test",
        expected_version=income["version"],
    )
    repository.classify(
        fixed["id"],
        "freelance",
        "expense.business",
        "manual",
        1.0,
        "Test",
        expected_version=repository.get_transaction(fixed["id"])["version"],
    )
    repository.classify(
        usd_income["id"],
        "freelance",
        "income.freelance",
        "manual",
        1.0,
        "Test",
        expected_version=usd_income["version"],
    )
    rate = analyses.add_exchange_rate(
        "USD",
        "EUR",
        Decimal("0.90"),
        rate_date=date(2026, 7, 1),
        source="manual-test",
        source_version="v1",
        idempotency_key="usd-eur-july-2026",
    )
    repeated_rate = analyses.add_exchange_rate(
        "USD",
        "EUR",
        Decimal("0.90"),
        rate_date=date(2026, 7, 1),
        source="manual-test",
        source_version="v1",
        idempotency_key="usd-eur-july-2026",
    )
    assert rate["created"] is True
    assert repeated_rate["created"] is False

    definition = AnalysisDefinition(
        account_scope=AccountScope.selected([eur["id"], usd["id"], debt["id"]]),
        profile="freelance",
        booking_from="2026-07-01",
        booking_to="2026-07-31",
        base_currency="EUR",
    )
    saved = analyses.create_saved_analysis("monthly-overview", "Monatsübersicht", definition)
    execution = analyses.run_saved_analysis(
        saved["id"], tmp_path / "reports", as_of=date(2026, 7, 31)
    )
    result = execution["result"]

    assert result["contract_version"] == "account-eval-analysis-v1"
    cashflow = {row["currency"]: row for row in result["cashflow"]["by_currency"]}
    assert cashflow["EUR"]["net_minor"] == 90000
    assert cashflow["USD"]["net_minor"] == 10000
    assert result["cashflow"]["base_currency"] == {
        "currency": "EUR",
        "income_minor": 109000,
        "expense_minor": -10000,
        "net_minor": 99000,
        "transaction_count": 3,
        "complete": True,
        "missing_transaction_count": 0,
    }
    assert result["fixed_costs"]["transaction_count"] == 1
    assert result["fixed_costs"]["by_currency"][0]["net_minor"] == -10000
    assert result["trends"]["by_currency"][0]["month"] == "2026-07"
    assert result["trends"]["base_currency"][0]["net_minor"] == 99000
    assert result["net_worth"]["base_currency"]["net_worth_minor"] == 88000
    positions = {
        position["account_name"]: position for position in result["net_worth"]["positions"]
    }
    assert positions["Darlehen"]["contribution_minor"] == -30000
    assert positions["USD Vermögen"]["base_contribution_minor"] == 18000
    assert result["metadata"]["exchange_rates"][0]["id"] == rate["id"]

    json_path = next(path for path in execution["paths"] if path.endswith(".json"))
    xlsx_path = next(path for path in execution["paths"] if path.endswith(".xlsx"))
    html_path = next(path for path in execution["paths"] if path.endswith(".html"))
    assert json.loads(Path(json_path).read_text(encoding="utf-8")) == result
    xlsx_cashflow = pd.read_excel(xlsx_path, sheet_name="Cashflow")
    assert int(xlsx_cashflow["net_minor"].sum()) == 100000
    html = Path(html_path).read_text(encoding="utf-8")
    embedded = re.search(
        r'<script id="dagoberd-analysis" type="application/json">(.*?)</script>',
        html,
    )
    assert embedded is not None
    assert json.loads(embedded.group(1).replace("<\\/", "</")) == result
    with repository.database.connect() as connection:
        run = connection.execute(
            "SELECT * FROM ledger_analysis_runs WHERE id=?", (execution["run_id"],)
        ).fetchone()
    assert run["result_hash"] == execution["result_hash"]


def test_missing_rate_never_creates_misleading_base_total(tmp_path):
    _, organization, analyses = _services(tmp_path)
    usd = organization.create_manual_account(
        "USD",
        account_kind="savings",
        currency="USD",
        opening_balance=Decimal("100"),
        balance_date=date(2026, 7, 1),
        idempotency_key="missing-rate-account",
    )
    _booking(
        organization,
        usd["id"],
        "missing-rate-booking",
        "50",
        "Income",
        date(2026, 7, 2),
    )
    result = analyses.analyze(
        AnalysisDefinition(
            account_scope=AccountScope.selected([usd["id"]]),
            base_currency="EUR",
            booking_from="2026-07-01",
            booking_to="2026-07-31",
        ),
        as_of=date(2026, 7, 31),
    )

    assert result["cashflow"]["base_currency"]["complete"] is False
    assert result["cashflow"]["base_currency"]["net_minor"] is None
    assert result["net_worth"]["base_currency"]["complete"] is False
    assert result["net_worth"]["base_currency"]["net_worth_minor"] is None
    assert result["quality"]["unconverted_transaction_count"] == 1
    assert any("missing exchange rates" in warning for warning in result["quality"]["warnings"])


def test_saved_definition_is_versioned_and_relative_period_is_reproducible(tmp_path):
    _, _, analyses = _services(tmp_path)
    saved = analyses.create_saved_analysis(
        "quarter", "Quartal", AnalysisDefinition(relative_months=3)
    )
    updated_definition = AnalysisDefinition(
        relative_months=6, sections=("cashflow",), formats=("json",)
    )
    updated = analyses.update_saved_analysis(saved["id"], updated_definition, expected_version=1)
    assert updated["version"] == 2
    with pytest.raises(VersionConflictError):
        analyses.update_saved_analysis(saved["id"], updated_definition, expected_version=1)
    result = analyses.run_saved_analysis(saved["id"], tmp_path / "out", as_of=date(2026, 7, 19))[
        "result"
    ]
    assert result["metadata"]["booking_from"] == "2026-02-01"
    assert result["metadata"]["booking_to"] == "2026-07-19"
    assert "net_worth" not in result


def test_cli_and_mcp_share_saved_analysis_contract(tmp_path, capsys):
    database = tmp_path / "contract.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "analyses",
                "create",
                "all",
                "Alles",
                "--from",
                "2026-01-01",
                "--to",
                "2026-12-31",
                "--base-currency",
                "EUR",
                "--formats",
                "json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["definition"]["base_currency"] == "EUR"
    assert (
        main(
            [
                "--db",
                str(database),
                "analyses",
                "run",
                "all",
                "--as-of",
                "2026-12-31",
                "--formats",
                "json",
                "--output",
                str(tmp_path / "cli-output"),
            ]
        )
        == 0
    )
    cli_run = json.loads(capsys.readouterr().out)
    assert cli_run["result"]["contract_version"] == "account-eval-analysis-v1"

    config = tmp_path / "account-eval.toml"
    config.write_text(
        '[reporting]\noutput_dir = "mcp-output"\n',
        encoding="utf-8",
    )
    mcp = MCPService(config_path=str(config), db_path=str(database))
    response = mcp.execute(
        "run_saved_analysis",
        {
            "analysis": "all",
            "as_of": "2026-12-31",
            "formats": ["json"],
            "idempotency_key": "mcp-analysis-run-001",
        },
    )
    assert response["result"]["cashflow"] == cli_run["result"]["cashflow"]
    names = {definition[0] for definition in TOOL_DEFINITIONS}
    assert {"create_saved_analysis", "add_exchange_rate", "run_saved_analysis"}.issubset(names)
