from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from account_eval.ledger.models import NormalizedTransaction


class AccountProvider(ABC):
    """A provider can only discover accounts and read their bookings."""

    kind: str

    @abstractmethod
    def iter_transactions(self) -> Iterable[NormalizedTransaction]:
        """Yield normalized transaction states. Never mutate the remote account."""
