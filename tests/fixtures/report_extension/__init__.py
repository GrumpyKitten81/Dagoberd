from pathlib import Path

from dagoberd.reporting.extensions import ReportContext

API_VERSION = 1


def generate(context: ReportContext) -> list[Path]:
    from .render import render

    return render(context)
