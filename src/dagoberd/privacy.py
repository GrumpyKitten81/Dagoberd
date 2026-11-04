"""Central masking helpers for local agent and CLI surfaces."""

from __future__ import annotations

import re
from typing import Any


def mask_identifier(value: object, visible: int = 4) -> str | None:
    if value is None:
        return None
    text = str(value)
    if visible <= 0:
        return "…" if text else ""
    return text if len(text) <= visible else f"…{text[-visible:]}"


def mask_iban(value: object, visible: int = 4) -> str | None:
    if value is None:
        return None
    compact = re.sub(r"\s+", "", str(value))
    return mask_identifier(compact, visible)


def limit_text(value: object, maximum: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= maximum else f"{text[:maximum]}…"


def safe_financial_record(
    record: dict[str, Any],
    *,
    visible: int = 4,
    maximum_text: int = 500,
) -> dict[str, Any]:
    safe = dict(record)
    for field in (
        "account_external_id",
        "iban",
        "reference",
        "external_id",
    ):
        if field in safe:
            safe[field] = mask_identifier(safe[field], visible)
    for field in ("purpose", "counterparty", "comment"):
        if field in safe:
            safe[field] = limit_text(safe[field], maximum_text)
    return safe
