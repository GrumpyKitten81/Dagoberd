"""Human-mediated Comdirect TAN callbacks for the local CLI only."""

from __future__ import annotations

import getpass
import tempfile
from pathlib import Path


class LocalCLIChallengeHandler:
    def push_tan(self) -> str:
        input("Confirm PushTAN in the Comdirect app and press Enter. ")
        return ""

    def photo_tan(self, challenge: bytes) -> str:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="account-eval-phototan-",
                suffix=".png",
                delete=False,
            ) as handle:
                handle.write(challenge)
                path = Path(handle.name)
            print(f"PhotoTAN-Challenge lokal anzeigen: {path}")
            return getpass.getpass("Enter PhotoTAN: ")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def sms_tan(self) -> str:
        return getpass.getpass("Enter SMS TAN: ")
