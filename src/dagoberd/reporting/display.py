"""Presentation copies and typed Excel cells for built-in reports."""

from __future__ import annotations

from typing import Any

import pandas as pd

from dagoberd.date_display import (
    DATE_FIELDS,
    TIMESTAMP_FIELDS,
    DatePresentation,
    calendar_date,
    missing,
)


def display_frame(frame: pd.DataFrame, display: DatePresentation) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if column in DATE_FIELDS or column in TIMESTAMP_FIELDS:
            result[column] = result[column].map(
                lambda value, key=str(column): display.field(key, value)
            )
    return result


def write_excel_table(
    frame: pd.DataFrame, writer: Any, sheet_name: str, display: DatePresentation
) -> None:
    result = frame.copy()
    for column in result.columns:
        if column in DATE_FIELDS:
            result[column] = pd.Series(
                [
                    calendar_date(value)
                    if calendar_date(value) is not None
                    else None
                    if missing(value)
                    else value
                    for value in result[column]
                ],
                index=result.index,
                dtype=object,
            )
        elif column in TIMESTAMP_FIELDS:
            result[column] = result[column].map(display.timestamp)
    result.to_excel(writer, sheet_name=sheet_name, index=False)
    sheet = writer.sheets[sheet_name]
    for index, column in enumerate(result.columns, 1):
        if column in DATE_FIELDS:
            for row in range(2, len(result) + 2):
                cell = sheet.cell(row, index)
                if cell.is_date:
                    cell.number_format = display.excel_format()
