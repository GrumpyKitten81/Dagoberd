from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pandas as pd

REPORT_EXTENSION_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReportContext:
    transactions: pd.DataFrame
    history: pd.DataFrame | None
    metadata: dict[str, Any]
    output_dir: Path
    formats: tuple[str, ...]


@contextmanager
def load_report_extension(path: Path) -> Iterator[Callable[[ReportContext], list[Path]]]:
    if path.suffix != ".py" or not path.is_file():
        raise ValueError("Report extension must point to an existing Python file")
    module_name = f"_account_eval_report_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load report extension")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        if getattr(module, "API_VERSION", None) != REPORT_EXTENSION_API_VERSION:
            raise ValueError("Unsupported report extension API_VERSION")
        generate = getattr(module, "generate", None)
        if not callable(generate):
            raise ValueError("Report extension must provide generate(context)")
        yield cast(Callable[[ReportContext], list[Path]], generate)
    finally:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                del sys.modules[name]


def validate_report_artifacts(
    artifacts: list[Path], output_dir: Path, formats: tuple[str, ...]
) -> list[Path]:
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Report extension must return a nonempty list of artifact paths")
    paths: list[Path] = []
    for artifact in artifacts:
        if not isinstance(artifact, Path):
            raise ValueError("Report extension artifacts must be pathlib.Path values")
        path = artifact.resolve()
        if not path.is_relative_to(output_dir.resolve()) or not path.is_file():
            raise ValueError("Report extension artifact must exist inside its output directory")
        if path.name == "report.metadata.json" or path.suffix.removeprefix(".") not in formats:
            raise ValueError("Report extension returned an unexpected artifact format or name")
        if path in paths:
            raise ValueError("Report extension returned duplicate artifact paths")
        paths.append(path)
    if {path.suffix.removeprefix(".") for path in paths} != set(formats):
        raise ValueError("Report extension did not produce every requested format")
    return paths
