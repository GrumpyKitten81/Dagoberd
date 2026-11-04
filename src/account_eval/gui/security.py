"""Loopback and token checks for the local GUI."""

from __future__ import annotations

import secrets
import socket

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from account_eval.contracts import GUI_CONTRACT_VERSION, failure


def ensure_loopback(host: str) -> str:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("GUI host must be 127.0.0.1 or localhost")
    return host


def ensure_port_available(host: str, port: int) -> None:
    """Fail before opening a browser that would point at an older GUI process."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as error:
        raise ValueError(
            f"GUI port {port} is already in use; close the previous GUI "
            "or choose another port with --port"
        ) from error


class TokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("X-AccountEval-Token", ""), self.token
        ):
            return JSONResponse(
                failure(
                    "unauthorized",
                    "Missing or invalid GUI token",
                    contract_version=GUI_CONTRACT_VERSION,
                ),
                status_code=401,
            )
        return await call_next(request)


class StaticAssetCacheMiddleware(BaseHTTPMiddleware):
    """Keep the HTML fresh while immutable assets live at content-addressed URLs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path == "/":
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["Clear-Site-Data"] = '"cache"'
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
