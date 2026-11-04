"""Read-only account data providers."""

from .base import AccountProvider
from .comdirect import ComdirectProvider

__all__ = ["AccountProvider", "ComdirectProvider"]
