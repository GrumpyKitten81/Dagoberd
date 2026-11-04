import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def create_symlink() -> Callable[[Path, Path], None]:
    def create(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except OSError as error:
            permission_denied = error.errno in (errno.EPERM, errno.EACCES) or (
                getattr(error, "winerror", None) == 1314
            )
            if permission_denied and os.environ.get("ACCOUNT_EVAL_REQUIRE_SYMLINKS") != "1":
                pytest.skip("Symlink creation is unavailable: filesystem permission denied")
            raise

    return create
