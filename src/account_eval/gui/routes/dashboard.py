"""Read-only account overview endpoint."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, Request

from account_eval.contracts import GUI_CONTRACT_VERSION, success
from account_eval.gui.context import GuiContext

router = APIRouter(prefix="/api")
ACCOUNT_QUERY: list[str] = Query(default=[])


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.get("/dashboard")
def dashboard(
    request: Request,
    account_id: list[str] = ACCOUNT_QUERY,
    period: Literal["3m", "ytd", "12m", "all"] = "12m",
    as_of: date | None = None,
) -> dict:
    return success(
        _context(request)
        .dashboard()
        .view(account_ids=tuple(account_id), period=period, as_of=as_of),
        contract_version=GUI_CONTRACT_VERSION,
    )
