"""Shared presentation resources, independent of GUI and ledger services."""

from importlib.resources import files
from pathlib import Path


def styles_directory() -> Path:
    """Locate installed CSS resources for the local static-file server."""
    return Path(__file__).with_name("styles")


def report_styles() -> str:
    """Embed the same design foundations in self-contained HTML exports."""
    directory = files(__package__).joinpath("styles")
    return "\n".join(
        directory.joinpath(name).read_text(encoding="utf-8")
        for name in ("tokens.css", "components.css", "report.css")
    )
