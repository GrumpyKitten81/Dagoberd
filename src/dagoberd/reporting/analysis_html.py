"""Standalone HTML presentation of analysis results."""

from __future__ import annotations

import html
import json
from typing import Any

import pandas as pd

from dagoberd.date_display import DatePresentation, resolve_display
from dagoberd.reporting.display import display_frame
from dagoberd.ui import report_styles


def render_analysis_html(
    result: dict[str, Any],
    tables: dict[str, pd.DataFrame],
    *,
    display: DatePresentation | None = None,
) -> str:
    display = display or resolve_display()
    embedded = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Dagoberd Analysis</title><style>",
        report_styles(),
        '</style></head><body class="analysis-report"><main class="page">',
        '<header class="hero ui-card"><h1>Dagoberd Analysis</h1></header>',
        '<script id="dagoberd-analysis" type="application/json">',
        embedded,
        "</script>",
    ]
    for name, frame in tables.items():
        parts.extend(
            [
                f'<section class="report-section ui-card"><h2>{html.escape(name)}</h2>',
                '<div class="table-wrap">',
                display_frame(frame, display).to_html(
                    index=False, escape=True, classes=["ui-table", "data-table"], border=0
                ),
                "</div></section>",
            ]
        )
    parts.append("</main></body></html>")
    return "".join(parts)
