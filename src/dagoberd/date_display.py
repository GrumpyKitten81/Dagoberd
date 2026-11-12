"""Workspace-scoped date presentation; storage and parsing formats stay unchanged."""

from __future__ import annotations

import ctypes
import locale
import math
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from babel import Locale, UnknownLocaleError
from babel.dates import format_date, get_date_format, tokenize_pattern
from pydantic import BaseModel, ConfigDict, field_validator

PRESETS = {
    "iso": "%Y-%m-%d",
    "dmy-dot": "%d.%m.%Y",
    "dmy-slash": "%d/%m/%Y",
    "mdy-slash": "%m/%d/%Y",
    "ymd-slash": "%Y/%m/%d",
}
FALLBACK_PATTERN = PRESETS["dmy-dot"]
DATE_FIELDS = frozenset(
    {
        "date",
        "booking_date",
        "valuta_date",
        "document_date",
        "balance_date",
        "rate_date",
        "valuation_date",
        "as_of",
        "booking_from",
        "booking_to",
        "first_date",
        "last_date",
        "earliest_date",
        "latest_date",
    }
)
TIMESTAMP_FIELDS = frozenset(
    {
        "created_at",
        "updated_at",
        "generated_at",
        "executed_at",
        "expires_at",
        "imported_at",
        "authenticated_at",
        "completed_at",
        "started_at",
        "recorded_at",
        "last_balance_at",
        "published_at",
        "restored_at",
        "deleted_at",
        "uploaded_at",
        "checked_at",
    }
)


def canonical_locale(value: str) -> str:
    if value.split("-", 1)[0].casefold() in {"root", "und"} or not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*", value
    ):
        raise ValueError("display.locale must be a supported locale tag such as de-DE or en-GB")
    try:
        parsed = Locale.parse(value, sep="-")
    except (ValueError, UnknownLocaleError) as error:
        raise ValueError(
            "display.locale is not supported; use a locale such as de-DE or en-GB"
        ) from error
    return str(parsed).replace("_", "-")


def normalized_pattern(value: str) -> str:
    if value == "locale":
        return value
    if value in PRESETS:
        return PRESETS[value]
    tokens = re.findall(r"%[A-Za-z]", value)
    if sorted(tokens) != ["%Y", "%d", "%m"] or re.sub(r"%[Ymd]", "", value).strip("./-_ "):
        raise ValueError(
            "display.date_format must be a standard format or use %d, %m and %Y exactly once with ./-_ or space separators"
        )
    return value


class DisplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    locale: str | None = None
    date_format: str = "locale"

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str | None) -> str | None:
        return canonical_locale(value) if value is not None else None

    @field_validator("date_format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        normalized_pattern(value)
        return value

    def gui_settings(self) -> dict[str, str | None]:
        return {"locale": self.locale, "date_format": normalized_pattern(self.date_format)}


def native_locale() -> str | None:
    """Query native user settings without changing the process-wide C locale."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        if platform.system() == "Windows":
            dll = vars(ctypes).get("windll")
            if dll is None:
                return None
            buffer = ctypes.create_unicode_buffer(85)
            return (
                buffer.value if dll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)) else None
            )
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    return None


def _system_tag(value: str | None) -> str | None:
    if not value:
        return None
    base = value.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    try:
        return canonical_locale(base)
    except ValueError:
        return None


def system_locale() -> str | None:
    for variable in ("LC_ALL", "LC_TIME", "LANG"):
        if value := os.environ.get(variable):
            # Explicit C/POSIX/encoding-only settings select the documented fallback.
            return _system_tag(value)
    try:
        active = locale.getlocale(locale.LC_TIME)[0]
    except (ValueError, TypeError):
        active = None
    return _system_tag(active) or _system_tag(native_locale())


def missing(value: Any) -> bool:
    return (
        value is None
        or (isinstance(value, float) and math.isnan(value))
        or (isinstance(value, date) and not isinstance(value.year, int))
    )


def calendar_date(value: Any) -> date | None:
    if missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class DatePresentation:
    locale: str | None
    pattern: str | None

    def date(self, value: Any) -> str:
        parsed = calendar_date(value)
        if parsed is None:
            return "" if missing(value) else str(value)
        if self.pattern is not None:
            return (
                self.pattern.replace("%Y", f"{parsed.year:04}")
                .replace("%m", f"{parsed.month:02}")
                .replace("%d", f"{parsed.day:02}")
            )
        return format_date(
            parsed, format="short", locale=self.locale.replace("-", "_") if self.locale else None
        )

    def timestamp(self, value: Any) -> str:
        if missing(value):
            return ""
        raw = value.isoformat() if isinstance(value, datetime) else str(value)
        if calendar_date(raw) is not None:
            return self.date(raw)
        if not re.match(r"\d{4}-\d{2}-\d{2}[T ]", raw):
            return raw
        try:
            datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return self.date(raw[:10]) + raw[10:]

    def field(self, key: str, value: Any) -> Any:
        if key in {"dimensions", "dimension_value", "raw_payload", "source_payload", "payload"}:
            return value
        if key in DATE_FIELDS:
            return self.date(value)
        if key in TIMESTAMP_FIELDS:
            return self.timestamp(value)
        return self.record(value)

    def record(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.field(str(key), item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.record(item) for item in value]
        return value

    def excel_format(self) -> str:
        if self.pattern is not None:
            return "".join(
                {"%Y": "yyyy", "%m": "mm", "%d": "dd"}.get(part, '"' + part + '"')
                for part in re.split(r"(%[Ymd])", self.pattern)
                if part
            )
        parts = []
        for kind, token in tokenize_pattern(
            str(
                get_date_format(
                    "short", locale=self.locale.replace("-", "_") if self.locale else None
                )
            )
        ):
            if kind == "chars":
                parts.append('"' + str(token).replace('"', '""') + '"')
            else:
                assert isinstance(token, tuple)
                symbol, count = token
                if symbol not in {"y", "M", "L", "d"}:
                    return "dd.mm.yyyy"
                parts.append(
                    {
                        "y": "yy" if count == 2 else "yyyy",
                        "M": "m" * min(count, 2),
                        "L": "m" * min(count, 2),
                        "d": "d" * min(count, 2),
                    }[symbol]
                )
        return "".join(parts)


def resolve_display(config: DisplayConfig | None = None) -> DatePresentation:
    config = config or DisplayConfig()
    pattern = normalized_pattern(config.date_format)
    resolved_locale = config.locale or system_locale()
    return DatePresentation(
        resolved_locale,
        None
        if pattern == "locale" and resolved_locale
        else FALLBACK_PATTERN
        if pattern == "locale"
        else pattern,
    )
