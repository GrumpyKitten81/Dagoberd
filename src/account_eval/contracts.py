"""Public semantic contract versions and stable response envelopes."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from account_eval import __version__
from account_eval.ledger.migrations import CURRENT_SCHEMA_VERSION

CLI_CONTRACT_VERSION = "account-eval-cli-v3"
GUI_CONTRACT_VERSION = "account-eval-gui-v5"
MCP_CONTRACT_VERSION = "account-eval-mcp-v2"
PROVIDER_CONTRACT_VERSION = "account-eval-provider-v1"
IMPORT_CONTRACT_VERSION = "account-eval-import-v2"
ANALYSIS_CONTRACT_VERSION = "account-eval-analysis-v1"
EXPORT_CONTRACT_VERSION = "account-eval-portable-export-v2"
LLM_PREVIEW_CONTRACT_VERSION = "account-eval-llm-preview-v2"
DELIVERY_CONTRACT_VERSION = "account-eval-delivery-v1"


class ExitCode(IntEnum):
    OK = 0
    OPERATION_FAILED = 1
    USAGE = 2
    CONFIGURATION = 3
    AUTHENTICATION = 4
    VERSION_CONFLICT = 5
    EXTERNAL_SERVICE = 6
    INTEGRITY = 7


def versions() -> dict[str, Any]:
    return {
        "product_version": __version__,
        "ledger_schema_version": CURRENT_SCHEMA_VERSION,
        "contracts": {
            "cli": CLI_CONTRACT_VERSION,
            "gui": GUI_CONTRACT_VERSION,
            "mcp": MCP_CONTRACT_VERSION,
            "provider": PROVIDER_CONTRACT_VERSION,
            "import": IMPORT_CONTRACT_VERSION,
            "analysis": ANALYSIS_CONTRACT_VERSION,
            "export": EXPORT_CONTRACT_VERSION,
            "llm_preview": LLM_PREVIEW_CONTRACT_VERSION,
            "delivery": DELIVERY_CONTRACT_VERSION,
        },
    }


def success(
    data: Any,
    *,
    metadata: dict[str, Any] | None = None,
    contract_version: str = CLI_CONTRACT_VERSION,
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "ok": True,
        "data": data,
        "metadata": metadata or {},
    }


def failure(
    code: str,
    message: str,
    *,
    details: Any = None,
    contract_version: str = CLI_CONTRACT_VERSION,
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "ok": False,
        "error": {"code": code, "message": message, "details": details},
    }


def page(items: list[Any], *, next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": MCP_CONTRACT_VERSION,
        "items": items,
        "next_cursor": next_cursor,
    }
