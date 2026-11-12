"""Metadata and account option endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from dagoberd.contracts import GUI_CONTRACT_VERSION, success, versions
from dagoberd.gui.context import GuiContext
from dagoberd.gui.schemas import AccountOptionsRequest
from dagoberd.importers import available_sources
from dagoberd.ledger.models import (
    VALID_TRANSACTION_STATUS_TRANSITIONS,
    AccountType,
    TransactionStatus,
)

router = APIRouter(prefix="/api")


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.get("/meta")
def meta(request: Request) -> dict:
    context = _context(request)
    organization = context.organization()
    return success(
        {
            "versions": versions(),
            "display": context.loaded.config.display.gui_settings(),
            "accounts": context.repository().list_accounts(),
            "account_groups": context.repository().list_account_groups(),
            "profiles": organization.list_profile_categories(),
            "dimensions": organization.list_dimensions(),
            "tags": organization.list_tags(),
            "merchants": organization.list_merchants(),
            "account_types": [item.value for item in AccountType],
            "booking_statuses": [item.value for item in TransactionStatus],
            "status_transitions": {
                status.value: sorted(target.value for target in targets)
                for status, targets in VALID_TRANSACTION_STATUS_TRANSITIONS.items()
            },
            "import_sources": list(available_sources()),
            "import_profiles": sorted(context.loaded.config.imports.profiles),
            "comdirect_connections": sorted(context.loaded.config.comdirect.connections),
        },
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/accounts")
def accounts(request: Request) -> dict:
    return success(
        _context(request).repository().list_accounts(), contract_version=GUI_CONTRACT_VERSION
    )


@router.get("/accounts/{account_id}/options")
def account_options(account_id: str, request: Request) -> dict:
    return success(
        _context(request).organization().account_field_options_view(account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/accounts/{account_id}/options/selected")
def selected_account_options(account_id: str, request: Request) -> dict:
    """The stored whitelist itself, as opposed to the effective values a GUI may offer."""
    return success(
        _context(request).organization().list_account_field_options(account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.put("/accounts/{account_id}/options")
def set_account_options(account_id: str, payload: AccountOptionsRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .set_account_field_options(account_id, payload.field_kind, payload.codes),
        contract_version=GUI_CONTRACT_VERSION,
    )
