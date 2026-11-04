"""Minimise financial data before it is sent to an optional LLM."""

from __future__ import annotations

import re

from dagoberd.config import PrivacyConfig

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){10,30}\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b")


def redact_text(value: str | None, privacy: PrivacyConfig) -> str | None:
    if value is None:
        return None
    redacted = _IBAN.sub("[IBAN]", value)
    redacted = _LONG_NUMBER.sub("[NUMMER]", redacted)
    redacted = _EMAIL.sub("[E-MAIL]", redacted)
    return redacted[: privacy.max_text_length]
