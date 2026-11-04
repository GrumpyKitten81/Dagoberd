"""Small detached-process boundary used by the repository GUI control scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2 or arguments[1] != "gui":
        print("usage: python -m dagoberd.gui.daemon LOG gui [OPTIONS]", file=sys.stderr)
        return 2

    log_path = Path(arguments[0]).resolve()
    command = [sys.executable, "-m", "dagoberd.cli", *arguments[1:]]
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    print(process.pid, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
