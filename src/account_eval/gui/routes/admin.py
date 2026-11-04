"""Account and organization maintenance endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from account_eval.contracts import GUI_CONTRACT_VERSION, success
from account_eval.gui.context import GuiContext
from account_eval.gui.schemas import VersionedRequest

router = APIRouter(prefix="/api")


class NameRequest(BaseModel):
    name: str


class DimensionValueRequest(NameRequest):
    code: str


class MerchantAliasRequest(BaseModel):
    alias: str


class AccountRequest(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    expected_version: int = Field(ge=1)


class DimensionUpdateRequest(VersionedRequest):
    name: str | None = None
    active: bool | None = None


class AccountColumnsRequest(BaseModel):
    dimension_codes: list[str]


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, payload: AccountRequest, request: Request) -> dict:
    repository = _context(request).repository()
    version = payload.expected_version
    if payload.display_name is not None:
        version = repository.update_account_alias(
            account_id, payload.display_name, expected_version=version
        )
    if payload.enabled is not None:
        version = repository.set_account_enabled(
            account_id, payload.enabled, expected_version=version
        )
    return success(
        {"account_id": account_id, "version": version}, contract_version=GUI_CONTRACT_VERSION
    )


@router.post("/tags")
def create_tag(payload: NameRequest, request: Request) -> dict:
    return success(
        _context(request).organization().create_tag(payload.name),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/dimensions")
def create_dimension(payload: DimensionValueRequest, request: Request) -> dict:
    return success(
        _context(request).organization().create_dimension(payload.code, payload.name),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/dimensions/{code}/values")
def create_dimension_value(code: str, payload: DimensionValueRequest, request: Request) -> dict:
    return success(
        _context(request).organization().create_dimension_value(code, payload.code, payload.name),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.patch("/dimensions/{code}")
def update_dimension(code: str, payload: DimensionUpdateRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .update_dimension(
            code,
            name=payload.name,
            active=payload.active,
            expected_version=payload.expected_version,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/dimensions/{code}")
def delete_dimension(code: str, request: Request) -> dict:
    _context(request).organization().delete_dimension(code)
    return success({"code": code, "deleted": True}, contract_version=GUI_CONTRACT_VERSION)


@router.patch("/dimensions/{code}/values/{value_code}")
def update_dimension_value(
    code: str, value_code: str, payload: DimensionUpdateRequest, request: Request
) -> dict:
    return success(
        _context(request)
        .organization()
        .update_dimension_value(
            code,
            value_code,
            name=payload.name,
            active=payload.active,
            expected_version=payload.expected_version,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/dimensions/{code}/values/{value_code}")
def delete_dimension_value(code: str, value_code: str, request: Request) -> dict:
    _context(request).organization().delete_dimension_value(code, value_code)
    return success(
        {"dimension": code, "code": value_code, "deleted": True},
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/accounts/{account_id}/columns")
def account_columns(account_id: str, request: Request) -> dict:
    return success(
        _context(request).organization().list_account_columns(account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.put("/accounts/{account_id}/columns")
def set_account_columns(account_id: str, payload: AccountColumnsRequest, request: Request) -> dict:
    return success(
        _context(request).organization().set_account_columns(account_id, payload.dimension_codes),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/merchants")
def create_merchant(payload: NameRequest, request: Request) -> dict:
    return success(
        _context(request).organization().create_merchant(payload.name),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/merchants/{merchant}/aliases")
def add_merchant_alias(merchant: str, payload: MerchantAliasRequest, request: Request) -> dict:
    return success(
        _context(request).organization().add_merchant_alias(merchant, payload.alias),
        contract_version=GUI_CONTRACT_VERSION,
    )
