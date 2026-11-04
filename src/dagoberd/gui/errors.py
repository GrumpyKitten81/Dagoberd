"""Stable GUI error envelopes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dagoberd.contracts import GUI_CONTRACT_VERSION, failure
from dagoberd.ledger.repository import VersionConflictError
from dagoberd.llm.openai_compatible import LLMProviderError
from dagoberd.providers.comdirect import ComdirectAuthenticationError, ComdirectError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(VersionConflictError)
    async def version_conflict(request: Request, error: VersionConflictError) -> JSONResponse:
        transaction_id = request.path_params.get("transaction_id") or request.path_params.get("id")
        details: dict[str, object] = {}
        if error.transaction_ids:
            details["transaction_ids"] = list(error.transaction_ids)
        context = getattr(request.app.state, "context", None)
        if transaction_id and context is not None:
            with suppress(KeyError):
                details["current"] = context.repository().get_transaction(transaction_id)
        return JSONResponse(
            failure(
                "version_conflict",
                str(error),
                details=details,
                contract_version=GUI_CONTRACT_VERSION,
            ),
            status_code=409,
        )

    def handler(status: int, code: str) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def respond(_: Request, error: Exception) -> JSONResponse:
            return JSONResponse(
                failure(code, str(error), contract_version=GUI_CONTRACT_VERSION), status_code=status
            )

        return respond

    app.add_exception_handler(KeyError, handler(404, "not_found"))
    app.add_exception_handler(FileNotFoundError, handler(404, "not_found"))
    app.add_exception_handler(ValueError, handler(400, "invalid_request"))
    app.add_exception_handler(ComdirectAuthenticationError, handler(401, "authentication_failed"))
    app.add_exception_handler(ComdirectError, handler(502, "comdirect_failed"))
    app.add_exception_handler(LLMProviderError, handler(502, "llm_failed"))
