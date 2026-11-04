import json
from pathlib import Path

from account_eval.reporting.extensions import ReportContext


def render(context: ReportContext) -> list[Path]:
    path = context.output_dir / "transactions.json"
    path.write_text(
        json.dumps(
            {
                "ids": context.transactions["id"].tolist(),
                "history_ids": None if context.history is None else context.history["id"].tolist(),
                "metadata": context.metadata,
            }
        ),
        encoding="utf-8",
    )
    return [path]
