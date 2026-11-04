"""Resumable read-only Comdirect synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from dagoberd.ledger.models import to_minor_units
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.providers.comdirect import ComdirectError, ComdirectProvider


@dataclass(frozen=True, slots=True)
class SyncResult:
    sync_run_id: str
    imported: int
    skipped: int
    accounts: int
    updated: int = 0
    discovered_accounts: int = 0
    synchronized_accounts: int = 0
    skipped_accounts: int = 0


class ComdirectSyncService:
    def __init__(
        self,
        repository: LedgerRepository,
        provider: ComdirectProvider,
        connection_name: str,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.connection_name = connection_name

    def synchronize(
        self,
        *,
        account_ids: list[str] | None = None,
        booking_from: date | None = None,
        booking_to: date | None = None,
        resume_sync_run_id: str | None = None,
        interactive: bool = False,
    ) -> SyncResult:
        provider_id = self.repository.ensure_provider(
            "comdirect",
            f"Comdirect ({self.connection_name})",
        )
        discovered = self.provider.list_accounts(interactive=interactive)
        transaction_accounts = [
            account for account in discovered if self.provider.account_type(account) is not None
        ]
        source_by_external: dict[str, str] = {}
        target_by_external: dict[str, str] = {}
        balances_by_external: dict[str, tuple[int | None, date | None]] = {}
        created_accounts = 0
        for remote_account in transaction_accounts:
            account_view = ComdirectProvider.balance_record(remote_account)
            external_id = account_view["accountId"]
            display_name = account_view.get("accountDisplayId") or external_id
            currency = self._account_currency(account_view)
            internal_id, created = self.repository.ensure_account(
                provider_id,
                external_id,
                display_name,
                currency,
                account_kind=self.provider.account_type(remote_account),
            )
            source_by_external[external_id] = internal_id
            target_by_external[external_id] = self.repository.routed_account_id(internal_id)
            created_accounts += int(created)
            balances_by_external[external_id] = (
                self._account_balance(account_view),
                self._account_balance_date(account_view),
            )

        selected_external = self._selected_external_accounts(
            account_ids,
            source_by_external,
            target_by_external,
        )
        selected_internal = [
            source_by_external[external_id] for external_id in sorted(selected_external)
        ]
        cursor: dict[str, int] = {}
        if resume_sync_run_id:
            previous = self.repository.get_sync_run(resume_sync_run_id)
            if previous["status"] != "failed":
                raise ValueError("Only failed sync runs can be resumed")
            cursor = {
                key: int(value)
                for key, value in previous["cursor"].items()
                if key in selected_external
            }
            if booking_from is None and previous["booking_from"]:
                booking_from = date.fromisoformat(previous["booking_from"])
            if booking_to is None and previous["booking_to"]:
                booking_to = date.fromisoformat(previous["booking_to"])

        run_id = self.repository.start_sync_run(
            provider_id,
            selected_internal,
            booking_from.isoformat() if booking_from else None,
            booking_to.isoformat() if booking_to else None,
            cursor=cursor,
            account_cursors={
                source_by_external[external_id]: offset
                for external_id, offset in cursor.items()
                if external_id in source_by_external
            },
        )
        imported = skipped = updated = 0
        try:
            for page in self.provider.iter_transaction_pages(
                accounts=transaction_accounts,
                account_external_ids=selected_external,
                booking_from=booking_from,
                booking_to=booking_to,
                start_offsets=cursor,
                interactive=interactive,
            ):
                result = self.repository.import_transactions(
                    "comdirect",
                    f"Comdirect ({self.connection_name})",
                    page.transactions,
                )
                imported += result.imported
                skipped += result.skipped
                updated += result.updated
                cursor[page.account_external_id] = page.next_offset
                self.repository.update_sync_run(
                    run_id,
                    account_id=source_by_external[page.account_external_id],
                    account_cursor=page.next_offset,
                    cursor=cursor,
                    imported_increment=result.imported,
                    skipped_increment=result.skipped,
                    updated_increment=result.updated,
                )
        except Exception as error:
            self.repository.finish_sync_run(
                run_id,
                status="failed",
                error_code=(
                    error.code if isinstance(error, ComdirectError) else error.__class__.__name__
                ),
            )
            raise
        for external_id in selected_external:
            balance, balance_date = balances_by_external.get(
                external_id,
                (None, None),
            )
            if balance is not None:
                self.repository.update_account_balance(
                    target_by_external[external_id],
                    balance,
                    balance_date=balance_date,
                    source="comdirect",
                    external_id=external_id,
                )
        self.repository.finish_sync_run(run_id, status="completed")
        return SyncResult(
            run_id,
            imported,
            skipped,
            created_accounts,
            updated,
            discovered_accounts=len(discovered),
            synchronized_accounts=len(selected_external),
            skipped_accounts=len(discovered) - len(transaction_accounts),
        )

    def _selected_external_accounts(
        self,
        account_ids: list[str] | None,
        source_by_external: dict[str, str],
        target_by_external: dict[str, str],
    ) -> set[str]:
        if not account_ids:
            enabled = {
                account["id"] for account in self.repository.list_accounts(enabled_only=True)
            }
            return {
                external_id
                for external_id, target_id in target_by_external.items()
                if target_id in enabled
            }
        external_by_internal: dict[str, set[str]] = {}
        for external_id, source_id in source_by_external.items():
            external_by_internal.setdefault(source_id, set()).add(external_id)
            external_by_internal.setdefault(target_by_external[external_id], set()).add(external_id)
        missing = [
            account_id for account_id in account_ids if account_id not in external_by_internal
        ]
        if missing:
            raise ValueError("Unknown or non-Comdirect accounts: " + ", ".join(missing))
        return {
            external_id
            for account_id in account_ids
            for external_id in external_by_internal[account_id]
        }

    @staticmethod
    def _account_currency(account: dict) -> str:
        for field in ("balance", "availableCashAmount"):
            amount = account.get(field)
            if amount and amount.get("unit"):
                return amount["unit"]
        return "EUR"

    @staticmethod
    def _account_balance(account: dict) -> int | None:
        for field in ("balance", "availableCashAmount"):
            amount = account.get(field)
            if amount and amount.get("value") is not None:
                return to_minor_units(Decimal(str(amount["value"])))
        return None

    @staticmethod
    def _account_balance_date(account: dict) -> date | None:
        candidates = [account.get("balanceDate")]
        for field in ("balance", "availableCashAmount"):
            amount = account.get(field) or {}
            candidates.extend((amount.get("date"), amount.get("valueDate")))
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return date.fromisoformat(str(candidate)[:10])
            except ValueError:
                continue
        return None
