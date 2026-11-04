"""Interactive standalone HTML reports for profile-based ledger exports."""

from __future__ import annotations

import html
import json
from decimal import Decimal
from typing import Any

import pandas as pd

PROFILE_TITLES: dict[str, str] = {
    "rental": "Rental report",
    "household": "Household ledger",
    "spain-tax": "Spanish tax preparation",
    "freelance": "Freelance report",
}


def profile_report_title(profile_code: str | None) -> str:
    if not profile_code:
        return "AccountEval Report"
    return PROFILE_TITLES.get(profile_code, "AccountEval Report")


def _escape(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return html.escape(json.dumps(value, ensure_ascii=False, default=str))
    return html.escape(str(value))


def _format_amount(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float, Decimal)):
        return f"{float(value):,.2f}"
    if isinstance(value, str):
        try:
            return f"{float(value):,.2f}"
        except ValueError:
            return _escape(value)
    return _escape(value)


def _dimension_value(row: pd.Series, code: str) -> str:
    dimensions = row.get("dimensions")
    if not isinstance(dimensions, dict):
        return ""
    value = dimensions.get(code)
    return "" if value is None else str(value)


def _tags_value(row: pd.Series) -> str:
    tags = row.get("tags")
    if isinstance(tags, list):
        return ", ".join(str(tag) for tag in tags)
    if isinstance(tags, str):
        return tags
    return ""


def prepare_transactions_display(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "Date",
                "Purpose",
                "Counterparty",
                "Amount",
                "Cashflow",
                "Category",
                "Person",
                "Property",
                "Jurisdiction",
                "Status",
                "Document",
                "Tags",
            ]
        )
    display = pd.DataFrame(
        {
            "Date": frame["booking_date"].dt.strftime("%Y-%m-%d"),
            "Purpose": frame["purpose"].fillna(""),
            "Counterparty": frame.get("counterparty", pd.Series([""] * len(frame))).fillna(""),
            "Amount": frame["amount"].map(_format_amount),
            "Cashflow": frame["cashflow_amount"].map(_format_amount),
            "Category": frame.get("category", pd.Series([""] * len(frame))).fillna(""),
            "Person": frame.apply(lambda row: _dimension_value(row, "person"), axis=1),
            "Property": frame.apply(lambda row: _dimension_value(row, "property"), axis=1),
            "Jurisdiction": frame.apply(lambda row: _dimension_value(row, "jurisdiction"), axis=1),
            "Status": frame.get("booking_status", pd.Series([""] * len(frame))).fillna(""),
            "Document": frame.get("document_status", pd.Series([""] * len(frame))).fillna(""),
            "Tags": frame.apply(_tags_value, axis=1),
        }
    )
    return display


def _kpi_cards(metadata: dict[str, Any], accounts: pd.DataFrame) -> str:
    income = ""
    expense = ""
    balance = ""
    if not accounts.empty:
        income = _format_amount(accounts["Income"].sum())
        expense = _format_amount(accounts["Expenses"].sum())
        balance = _format_amount(accounts["Balance"].sum())
    period = "-"
    if metadata.get("booking_from") or metadata.get("booking_to"):
        period = f"{metadata.get('booking_from') or '...'} - {metadata.get('booking_to') or '...'}"
    cards = [
        ("Transactions", str(metadata.get("transaction_count", 0))),
        ("Period", period),
        ("Income", income),
        ("Expenses", expense),
        ("Balance", balance),
    ]
    items = "".join(
        f'<article class="kpi-card"><span class="kpi-label">{_escape(label)}</span>'
        f'<strong class="kpi-value">{_escape(value)}</strong></article>'
        for label, value in cards
    )
    return f'<section class="kpi-grid">{items}</section>'


def _metadata_rows(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for key, value in metadata.items():
        if isinstance(value, (dict, list, tuple)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            rendered = "" if value is None else str(value)
        rows.append((key, rendered))
    return rows


def _simple_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return '<p class="empty">No data available.</p>'
    headers = "".join(f"<th>{_escape(column)}</th>" for column in frame.columns)
    body_rows = []
    for _, row in frame.iterrows():
        cells = "".join(f"<td>{_escape(value)}</td>" for value in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap"><table class="data-table">'
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _filterable_transactions_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return '<p class="empty">No transactions available.</p>'
    headers = "".join(f"<th>{_escape(column)}</th>" for column in frame.columns)
    filters = "".join(
        '<th><input type="search" class="column-filter" data-filter '
        f'placeholder="{_escape(column)}"></th>'
        for column in frame.columns
    )
    body_rows = []
    for _, row in frame.iterrows():
        cells = "".join(f"<td>{_escape(value)}</td>" for value in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap">'
        '<table class="data-table filterable-table" id="transactions-table">'
        f'<thead><tr>{headers}</tr><tr class="filter-row">{filters}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
        '<p class="table-meta"><span id="transactions-visible-count"></span></p>'
        "</div>"
    )


def _details_section(section_id: str, title: str, preview: str, body: str) -> str:
    return (
        f'<details class="report-section" id="{section_id}">'
        f'<summary><span class="section-title">{_escape(title)}</span>'
        f'<span class="section-preview">{_escape(preview)}</span></summary>'
        f'<div class="section-body">{body}</div>'
        "</details>"
    )


def render_profile_report_html(
    *,
    title: str,
    metadata: dict[str, Any],
    categories: pd.DataFrame,
    months: pd.DataFrame,
    accounts: pd.DataFrame,
    open_items: pd.DataFrame,
    transactions: pd.DataFrame,
) -> str:
    transactions_display = prepare_transactions_display(transactions)
    metadata_table = _simple_table(
        pd.DataFrame(_metadata_rows(metadata), columns=["Field", "Value"])
    )
    sections = [
        _details_section(
            "metadata",
            "Metadata",
            f"{len(metadata)} fields",
            metadata_table,
        ),
        _details_section(
            "categories",
            "Categories",
            f"{len(categories)} rows",
            _simple_table(categories),
        ),
        _details_section(
            "months",
            "Months",
            f"{len(months)} rows",
            _simple_table(months),
        ),
        _details_section(
            "accounts",
            "Accounts",
            f"{len(accounts)} rows",
            _simple_table(accounts),
        ),
        _details_section(
            "open-items",
            "Open items",
            f"{len(open_items)} rows",
            _simple_table(open_items),
        ),
        _details_section(
            "transactions",
            "Transactions",
            f"{len(transactions_display)} rows",
            _filterable_transactions_table(transactions_display),
        ),
    ]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  <style>
    :root {{
      --bg: #f5f6f8;
      --surface: #ffffff;
      --text: #1f2933;
      --muted: #5f6b7a;
      --border: #d9dee7;
      --accent: #245bdb;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .page {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    .hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
      padding: 24px;
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.8rem;
      font-weight: 650;
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-top: 20px;
    }}
    .kpi-card {{
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
    }}
    .kpi-label {{
      display: block;
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 6px;
    }}
    .kpi-value {{
      font-size: 1.2rem;
      font-variant-numeric: tabular-nums;
    }}
    .report-section {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      margin-bottom: 12px;
      overflow: hidden;
    }}
    .report-section > summary {{
      list-style: none;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 16px 18px;
      font-weight: 600;
    }}
    .report-section > summary::-webkit-details-marker {{
      display: none;
    }}
    .report-section > summary::before {{
      content: "▸";
      color: var(--accent);
      margin-right: 10px;
      transition: transform 0.15s ease;
    }}
    .report-section[open] > summary::before {{
      transform: rotate(90deg);
    }}
    .section-preview {{
      color: var(--muted);
      font-weight: 500;
      font-size: 0.9rem;
    }}
    .section-body {{
      padding: 0 18px 18px;
      border-top: 1px solid var(--border);
    }}
    .table-wrap {{
      overflow-x: auto;
      margin-top: 14px;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.92rem;
    }}
    .data-table th,
    .data-table td {{
      border-bottom: 1px solid var(--border);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }}
    .data-table th {{
      background: #f8fafc;
      font-weight: 600;
      white-space: nowrap;
    }}
    .data-table tbody tr:hover {{
      background: #f8fafc;
    }}
    .filter-row input {{
      width: 100%;
      min-width: 90px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 8px;
      font: inherit;
      background: #fff;
    }}
    .table-meta {{
      margin: 10px 2px 0;
      color: var(--muted);
      font-size: 0.85rem;
    }}
    .empty {{
      margin: 14px 0 0;
      color: var(--muted);
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 20px 12px 32px; }}
      .report-section > summary {{
        flex-direction: column;
        align-items: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <h1>{_escape(title)}</h1>
      <p class="subtitle">Generated at {_escape(metadata.get("generated_at", ""))}</p>
      {_kpi_cards(metadata, accounts)}
    </header>
    {"".join(sections)}
  </div>
  <script>
    (function () {{
      const table = document.getElementById("transactions-table");
      const counter = document.getElementById("transactions-visible-count");
      if (!table || !counter) {{
        return;
      }}
      const filters = Array.from(table.querySelectorAll(".column-filter"));
      const rows = Array.from(table.querySelectorAll("tbody tr"));
      function updateCount(visible) {{
        counter.textContent = visible + " of " + rows.length + " transactions visible";
      }}
      function applyFilters() {{
        let visible = 0;
        for (const row of rows) {{
          let show = true;
          for (let index = 0; index < filters.length; index += 1) {{
            const query = filters[index].value.trim().toLowerCase();
            if (!query) {{
              continue;
            }}
            const cell = row.cells[index];
            const text = (cell ? cell.textContent : "").toLowerCase();
            if (!text.includes(query)) {{
              show = false;
              break;
            }}
          }}
          row.style.display = show ? "" : "none";
          if (show) {{
            visible += 1;
          }}
        }}
        updateCount(visible);
      }}
      for (const filter of filters) {{
        filter.addEventListener("input", applyFilters);
      }}
      applyFilters();
    }})();
  </script>
</body>
</html>"""
