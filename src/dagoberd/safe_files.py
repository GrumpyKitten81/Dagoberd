from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def prepared_new_file(destination: Path) -> Iterator[Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Destination already exists: {destination}")
    with tempfile.TemporaryDirectory(
        prefix=".dagoberd-write-", dir=destination.parent
    ) as temporary:
        staged = Path(temporary) / "payload"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        yield staged
        with staged.open("r+b") as handle:
            os.fsync(handle.fileno())
        try:
            # DESIGN: Hard links publish atomically without replacing a competing destination.
            os.link(staged, destination)
        except FileExistsError as error:
            raise FileExistsError(f"Destination already exists: {destination}") from error
