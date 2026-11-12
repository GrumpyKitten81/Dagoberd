from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from dagoberd import cli, date_display
from dagoberd.config import DEFAULT_CONFIG_TOML, AppConfig, load_config
from dagoberd.date_display import DisplayConfig, resolve_display
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService
from dagoberd.reporting.analysis_html import render_analysis_html
from dagoberd.reporting.display import write_excel_table
from dagoberd.reporting.ledger_reports import LedgerReportGenerator


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("iso", "2026-07-01"),
        ("dmy-dot", "01.07.2026"),
        ("dmy-slash", "01/07/2026"),
        ("mdy-slash", "07/01/2026"),
        ("ymd-slash", "2026/07/01"),
        ("%m_%d_%Y", "07_01_2026"),
        ("%Y%m%d", "20260701"),
        ("%d %m %Y", "01 07 2026"),
    ],
)
def test_presets_and_custom_patterns(pattern, expected):
    config = DisplayConfig(locale="en-US", date_format=pattern)
    formatter = resolve_display(config)
    assert formatter.date("2026-07-01") == expected
    assert formatter.date(date(2026, 7, 1)) == expected
    assert config.gui_settings()["date_format"].startswith("%")


@pytest.mark.parametrize(
    "value",
    ["", "%d.%m", "%d.%m.%y", "%d.%m.%Y-%Y", "%B %d %Y", "%d\n%m\n%Y", "%Y<script>%m%d", "unknown"],
)
def test_invalid_formats_rejected(value):
    with pytest.raises(ValidationError, match=r"display\.date_format"):
        DisplayConfig(date_format=value)


@pytest.mark.parametrize(
    "value", ["", "C", "POSIX", "de_DE.UTF-8", "not-a-locale", "en-US-u-ca-buddhist"]
)
def test_invalid_configured_locales_rejected(value):
    with pytest.raises(ValidationError, match=r"display\.locale"):
        DisplayConfig(locale=value)


@pytest.mark.parametrize(
    "locale,expected",
    [("de-DE", "01.07.26"), ("es-ES", "1/7/26"), ("en-US", "7/1/26"), ("en-GB", "01/07/2026")],
)
def test_locale_short_format_and_configuration_priority(monkeypatch, locale, expected):
    monkeypatch.setattr(date_display, "system_locale", lambda: "en-US")
    assert resolve_display(DisplayConfig(locale=locale)).date("2026-07-01") == expected
    assert (
        resolve_display(DisplayConfig(locale=locale, date_format="iso")).date("2026-07-01")
        == "2026-07-01"
    )


def test_locale_resolution_precedence_and_fallback(monkeypatch):
    for name in ["LC_ALL", "LC_TIME", "LANG"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(date_display.locale, "getlocale", lambda category: (None, None))
    monkeypatch.setattr(date_display, "native_locale", lambda: "es_ES")
    assert date_display.system_locale() == "es-ES"
    monkeypatch.setattr(date_display.locale, "getlocale", lambda category: ("de_DE", "UTF-8"))
    assert date_display.system_locale() == "de-DE"
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert date_display.system_locale() == "en-GB"
    monkeypatch.setenv("LC_TIME", "de_DE.UTF-8")
    assert date_display.system_locale() == "de-DE"
    monkeypatch.setenv("LC_ALL", "es_ES.UTF-8")
    assert date_display.system_locale() == "es-ES"
    for unsupported in ["C", "POSIX", "C.UTF-8", "UTF-8", "nonsense"]:
        monkeypatch.setenv("LC_ALL", unsupported)
        assert date_display.system_locale() is None
        assert resolve_display().date("2026-07-01") == "01.07.2026"


def test_native_providers_and_failures(monkeypatch):
    monkeypatch.setattr(date_display.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        date_display.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="de_DE\n"),
    )
    assert date_display.native_locale() == "de_DE"

    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(date_display.subprocess, "run", fail)
    assert date_display.native_locale() is None
    monkeypatch.setattr(date_display.platform, "system", lambda: "Windows")

    def windows_name(buffer, size):
        buffer.value = "en-GB"
        return len(buffer.value) + 1

    monkeypatch.setattr(
        date_display.ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetUserDefaultLocaleName=windows_name)),
        raising=False,
    )
    assert date_display.native_locale() == "en-GB"


def test_dates_and_timestamp_offsets_are_not_shifted():
    display = resolve_display(DisplayConfig(date_format="dmy-dot"))
    for raw, expected in [
        ("2024-02-29", "29.02.2024"),
        ("2026-12-31", "31.12.2026"),
        ("0001-01-01", "01.01.0001"),
    ]:
        assert display.date(raw) == expected
    for raw in [
        "2026-07-01T00:15:00+14:00",
        "2026-12-31T23:30:00-12:00",
        "2026-03-29T01:30:00+01:00",
        "2026-10-25T02:30:00+02:00",
    ]:
        assert display.timestamp(raw) == display.date(raw[:10]) + raw[10:]
        assert display.timestamp(datetime.fromisoformat(raw)) == display.date(raw[:10]) + raw[10:]
    for invalid in ["2026-02-30", "not a date", "2026-13-01"]:
        assert display.date(invalid) == invalid
        assert display.timestamp(invalid) == invalid
    assert display.date(None) == ""
    assert display.date(pd.NaT) == ""
    assert display.date(float("nan")) == ""


def test_config_defaults_local_override_and_import_formats(tmp_path):
    path = tmp_path / "dagoberd.toml"
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    local = tmp_path / "dagoberd.local.toml"
    local.write_text('[display]\nlocale="es-ES"\ndate_format="dmy-dot"\n', encoding="utf-8")
    loaded = load_config(path, use_environment=False)
    assert loaded.config.display == DisplayConfig(locale="es-ES", date_format="dmy-dot")
    assert loaded.config.imports.profiles == AppConfig().imports.profiles


def test_human_records_leave_free_text_paths_and_periods_unchanged():
    display = resolve_display(DisplayConfig(date_format="dmy-dot"))
    raw = {
        "booking_date": "2026-07-01",
        "purpose": "2026-07-01",
        "path": "/2026-07-01/report",
        "month": "2026-07",
        "dimensions": {"date": "2026-07-01"},
        "details": [{"created_at": "2026-07-01T12:00:00Z"}],
    }
    result = display.record(raw)
    assert result["booking_date"] == "01.07.2026"
    assert result["details"][0]["created_at"] == "01.07.2026T12:00:00Z"
    assert all(result[key] == raw[key] for key in ["purpose", "path", "month", "dimensions"])
    assert raw["booking_date"] == "2026-07-01"


def synthetic_ledger(tmp_path):
    config = tmp_path / "dagoberd.toml"
    config.write_text(
        '[database]\npath="ledger.db"\n[display]\ndate_format="dmy-dot"\n', encoding="utf-8"
    )
    loaded = load_config(config, use_environment=False)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    organization = OrganizationService(repository)
    account = organization.create_manual_account(
        "Synthetic cash",
        account_kind="cash",
        opening_balance=Decimal("100"),
        balance_date=date(2026, 7, 1),
        idempotency_key="date-display-account",
    )
    organization.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 2),
        amount=Decimal("-1"),
        purpose="2026-07-02",
        category_profile="household",
        category_code="expense.food",
        idempotency_key="date-display-booking",
    )
    return config, loaded, repository, account


def test_cli_human_json_legacy_and_invocation_context(tmp_path, capsys, monkeypatch):
    config, _, _, _ = synthetic_ledger(tmp_path)
    monkeypatch.setattr(date_display, "system_locale", lambda: None)
    for mode in ["human", "json", "legacy-json", "human"]:
        assert cli.main(["--config", str(config), "--output", mode, "transactions"]) == 0
        text = capsys.readouterr().out
        if mode == "human":
            assert "booking_date: 02.07.2026" in text and "purpose: 2026-07-02" in text
        else:
            data = json.loads(text)
            rows = data["data"] if mode == "json" else data
            assert rows[0]["booking_date"] == "2026-07-02"
        assert cli._DISPLAY.get() is None
    config.write_text(config.read_text().replace('"dmy-dot"', '"iso"'), encoding="utf-8")
    assert cli.main(["--config", str(config), "--output", "human", "transactions"]) == 0
    assert "booking_date: 2026-07-02" in capsys.readouterr().out


def test_profile_and_analysis_reports_keep_raw_data_and_typed_excel_dates(tmp_path):
    _, loaded, repository, _ = synthetic_ledger(tmp_path)
    reports = LedgerReportGenerator(repository, tmp_path / "reports", display=loaded.config.display)
    paths = reports.generate("household", booking_from="2026-07-01", booking_to="2026-07-31")
    html = next(path for path in paths if path.suffix == ".html").read_text()
    assert "02.07.2026" in html and "01.07.2026 - 31.07.2026" in html
    data = json.loads(next(path for path in paths if path.suffix == ".json").read_text())
    assert data["transactions"][0]["booking_date"].startswith("2026-07-02")
    workbook = load_workbook(next(path for path in paths if path.suffix == ".xlsx"))
    sheet = workbook["Transactions"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    cell = sheet.cell(2, columns["booking_date"])
    assert cell.value == datetime(2026, 7, 2) and cell.is_date and "yyyy" in cell.number_format
    assert sheet.cell(2, columns["purpose"]).value == "2026-07-02"
    analysis = AnalysisService(repository, display=loaded.config.display)
    result = analysis.analyze(
        AnalysisDefinition(booking_from="2026-07-01", booking_to="2026-07-31")
    )
    table = pd.DataFrame(
        [
            {
                "booking_date": "2026-07-02",
                "created_at": "2026-07-02T12:30:00+02:00",
                "purpose": "2026-07-02",
            }
        ]
    )
    rendered = render_analysis_html(
        result, {"Synthetic": table}, display=resolve_display(loaded.config.display)
    )
    assert "02.07.2026T12:30:00+02:00" in rendered
    assert result["metadata"]["booking_from"] == "2026-07-01"
    saved = analysis.create_saved_analysis(
        "dated",
        "Synthetic date report",
        AnalysisDefinition(booking_from="2026-07-01", booking_to="2026-07-31"),
    )
    execution = analysis.run_saved_analysis(
        saved["id"], tmp_path / "analyses", as_of=date(2026, 7, 31)
    )
    analysis_paths = [Path(path) for path in execution["paths"]]
    analysis_html = next(path for path in analysis_paths if path.suffix == ".html").read_text()
    assert "01.07.2026" in analysis_html
    analysis_json = json.loads(
        next(path for path in analysis_paths if path.suffix == ".json").read_text()
    )
    assert analysis_json["metadata"]["booking_from"] == "2026-07-01"
    analysis_sheet = load_workbook(next(path for path in analysis_paths if path.suffix == ".xlsx"))[
        "Metadata"
    ]
    analysis_columns = {cell.value: cell.column for cell in analysis_sheet[1]}
    assert analysis_sheet.cell(2, analysis_columns["booking_from"]).value == datetime(2026, 7, 1)

    with pd.ExcelWriter(tmp_path / "dates.xlsx", engine="openpyxl") as writer:
        write_excel_table(table, writer, "Dates", resolve_display(loaded.config.display))
    sheet = load_workbook(tmp_path / "dates.xlsx")["Dates"]
    assert sheet["A2"].is_date
    assert sheet["B2"].value == "02.07.2026T12:30:00+02:00"
    assert table.iloc[0]["booking_date"] == "2026-07-02"
