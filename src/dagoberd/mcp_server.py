"""Local, client-neutral stdio MCP server for Dagoberd."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from dagoberd.accounts import AccountScope
from dagoberd.config import comdirect_timed_session_secret, masked_config
from dagoberd.contracts import versions
from dagoberd.doctor import run_diagnostics
from dagoberd.documents.batch import DocumentBatchService
from dagoberd.documents.extraction import create_local_ocr_provider
from dagoberd.documents.matching import DocumentMatcher
from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import TransactionSearchFilter, to_minor_units
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError
from dagoberd.ledger.semantics import TransactionSemanticsService
from dagoberd.llm import create_llm_provider
from dagoberd.llm.openai_compatible import LLMProviderError
from dagoberd.privacy import safe_financial_record
from dagoberd.profiles import install_builtin_profiles
from dagoberd.providers.comdirect import ComdirectProvider, ComdirectSessionState
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.secrets import SecretStore
from dagoberd.services.comdirect_diagnostics import ComdirectDiagnosticsService
from dagoberd.services.comdirect_sync import ComdirectSyncService
from dagoberd.services.import_service import ImportService
from dagoberd.services.llm_preview import LLMPreviewService
from dagoberd.services.portability import PortabilityService
from dagoberd.services.report_delivery import ReportDeliveryService
from dagoberd.workspaces import WorkspaceRegistry, resolve_workspace, workspace_operation


def _schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


TOOL_DEFINITIONS = (
    ("get_status", "Local configuration and system status without network access.", _schema()),
    ("system_status", "Local diagnostic status without network access.", _schema()),
    ("config_summary", "Masked project configuration without resolving secrets.", _schema()),
    ("contract_versions", "Product, schema and interface versions.", _schema()),
    (
        "list_accounts",
        "List local accounts with masked external IDs.",
        _schema({"enabled_only": {"type": "boolean"}}),
    ),
    (
        "list_transactions",
        "List at most 500 masked transactions with optional account, status and period filters.",
        _schema(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "booking_from": {"type": "string"},
                "booking_to": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "profile": {"type": "string"},
                "booking_statuses": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["pending", "booked", "reversed", "corrected"],
                    },
                },
            }
        ),
    ),
    (
        "list_transaction_history",
        "List immutable transaction status history.",
        _schema({"transaction_id": {"type": "string"}}, ["transaction_id"]),
    ),
    (
        "transition_transaction_status",
        "Change booking status with transition and version checks.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "booking_status": {
                    "type": "string",
                    "enum": ["pending", "booked", "reversed", "corrected"],
                },
                "expected_version": {"type": "integer"},
                "reason": {"type": "string"},
            },
            ["transaction_id", "booking_status", "expected_version", "reason"],
        ),
    ),
    (
        "suggest_transaction_relations",
        "Create explainable transfer, refund, reversal or correction candidates.",
        _schema(
            {
                "relation_type": {
                    "type": "string",
                    "enum": ["transfer", "refund", "reversal", "correction"],
                },
                "transaction_id": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["relation_type"],
        ),
    ),
    (
        "list_transaction_relations",
        "List versioned transaction relations and candidates.",
        _schema(
            {
                "relation_type": {
                    "type": "string",
                    "enum": ["transfer", "refund", "reversal", "correction"],
                },
                "status": {
                    "type": "string",
                    "enum": ["suggested", "confirmed", "rejected", "resolved"],
                },
                "transaction_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "create_transaction_relation",
        "Create a manual relation candidate with version protection.",
        _schema(
            {
                "relation_type": {
                    "type": "string",
                    "enum": ["transfer", "refund", "reversal", "correction"],
                },
                "source_transaction_id": {"type": "string"},
                "target_transaction_id": {"type": "string"},
                "expected_source_version": {"type": "integer"},
                "expected_target_version": {"type": "integer"},
                "fee": {"type": "string"},
                "reasons": {"type": "array", "items": {"type": "string"}},
            },
            [
                "relation_type",
                "source_transaction_id",
                "target_transaction_id",
                "expected_source_version",
                "expected_target_version",
            ],
        ),
    ),
    (
        "review_transaction_relation",
        "Accept, reject or unlink a relation with version protection.",
        _schema(
            {
                "relation_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["confirm", "reject", "resolve"]},
                "expected_version": {"type": "integer"},
            },
            ["relation_id", "decision", "expected_version"],
        ),
    ),
    (
        "record_balance_snapshot",
        "Store a local balance and reconcile it against the ledger.",
        _schema(
            {
                "account_id": {"type": "string"},
                "amount": {"type": "string"},
                "balance_date": {"type": "string"},
                "source": {"type": "string"},
                "external_id": {"type": "string"},
                "expected_version": {"type": "integer"},
            },
            ["account_id", "amount", "balance_date", "expected_version"],
        ),
    ),
    (
        "list_balance_snapshots",
        "List local balance snapshots.",
        _schema(
            {
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "reconcile_balance_snapshot",
        "Calculate idempotent account reconciliation for a snapshot.",
        _schema({"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
    ),
    (
        "list_reconciliations",
        "List account reconciliations and visible differences.",
        _schema(
            {
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "list_account_import_history",
        "List bounded import history for one account in the selected workspace.",
        _schema(
            {
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["account_id"],
        ),
    ),
    (
        "preview_account_import",
        "Persist a local import preview with structured errors and duplicates; does not modify transactions.",
        _schema(
            {
                "source": {"type": "string", "enum": ["camt053", "tabular", "wiso365"]},
                "path": {"type": "string"},
                "account_id": {"type": "string"},
                "profile": {"type": "string"},
            },
            ["source", "path"],
        ),
    ),
    (
        "import_account_file",
        "Execute a previously reviewed, unconsumed import preview.",
        _schema({"preview_id": {"type": "string"}}, ["preview_id"]),
    ),
    (
        "diagnose_comdirect",
        "Return sanitized session, configuration and recovery status without network calls.",
        _schema({"connection": {"type": "string"}}, ["connection"]),
    ),
    (
        "sync_comdirect",
        "Synchronize using the experimental Comdirect API after local authentication; MCP never handles TANs.",
        _schema(
            {
                "connection": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "booking_from": {"type": "string"},
                "booking_to": {"type": "string"},
                "resume_sync_run_id": {"type": "string"},
            },
            ["connection"],
        ),
    ),
    (
        "run_classification",
        "Run rules, history, hints and optionally an LLM for a transaction or account scope.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "profile": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
            },
            ["profile"],
        ),
    ),
    (
        "preview_llm_classification",
        "Store and show the exact redacted data before a local or cloud model request.",
        _schema(
            {
                "transaction_ids": {"type": "array", "items": {"type": "string"}},
                "profile": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
            },
            ["profile"],
        ),
    ),
    (
        "execute_llm_classification_preview",
        "Execute only an unchanged confirmed LLM preview; results remain suggestions.",
        _schema(
            {
                "preview_id": {"type": "string"},
                "fingerprint": {"type": "string"},
            },
            ["preview_id", "fingerprint"],
        ),
    ),
    (
        "list_classification_suggestions",
        "List pending classification suggestions for review.",
        _schema(
            {
                "profile": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "review_classification",
        "Accept or reject a suggestion with version protection.",
        _schema(
            {
                "classification_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject"]},
                "expected_version": {"type": "integer"},
            },
            ["classification_id", "decision", "expected_version"],
        ),
    ),
    (
        "classify_transaction",
        "Store a direct audited classification with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "profile": {"type": "string"},
                "category": {"type": "string"},
                "expected_version": {"type": "integer"},
                "rationale": {"type": "string"},
            },
            ["transaction_id", "profile", "category", "expected_version"],
        ),
    ),
    (
        "add_rule",
        "Create a deterministic profile rule.",
        _schema(
            {
                "profile": {"type": "string"},
                "name": {"type": "string"},
                "pattern": {"type": "string"},
                "category": {"type": "string"},
                "priority": {"type": "integer"},
            },
            ["profile", "name", "pattern", "category"],
        ),
    ),
    (
        "add_hint",
        "Create a user hint, optionally with a deterministic pattern.",
        _schema(
            {
                "profile": {"type": "string"},
                "text": {"type": "string"},
                "pattern": {"type": "string"},
                "category": {"type": "string"},
                "priority": {"type": "integer"},
            },
            ["profile", "text"],
        ),
    ),
    (
        "import_document",
        "Import a local document by hash into the protected document directory.",
        _schema(
            {
                "path": {"type": "string"},
                "amount": {"type": "string"},
                "date": {"type": "string"},
                "merchant": {"type": "string"},
                "reference": {"type": "string"},
                "account_id": {"type": "string"},
                "profile": {"type": "string"},
            },
            ["path"],
        ),
    ),
    (
        "list_documents",
        "List document metadata, never document contents.",
        _schema({"status": {"type": "string"}}),
    ),
    (
        "preview_document_batch",
        "Preview a local document batch without changing files or ledger data.",
        _schema({"path": {"type": "string"}}, ["path"]),
    ),
    (
        "run_document_batch",
        "Import exactly one unchanged fingerprint-bound document batch.",
        _schema(
            {"preview_id": {"type": "string"}, "fingerprint": {"type": "string"}},
            ["preview_id", "fingerprint"],
        ),
    ),
    (
        "preview_document_matches",
        "Calculate document candidates without writes and return a fingerprint.",
        _schema(
            {
                "document_id": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "profile": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["document_id"],
        ),
    ),
    (
        "apply_document_match_preview",
        "Link confirmed transactions from an unchanged document preview.",
        _schema(
            {
                "document_id": {"type": "string"},
                "fingerprint": {"type": "string"},
                "transaction_ids": {"type": "array", "items": {"type": "string"}},
                "expected_document_version": {"type": "integer"},
                "append": {"type": "boolean"},
            },
            [
                "document_id",
                "fingerprint",
                "transaction_ids",
                "expected_document_version",
            ],
        ),
    ),
    (
        "list_matches",
        "List bounded document-match metadata and scores.",
        _schema(
            {
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "match_document",
        "Calculate explainable document candidates.",
        _schema(
            {
                "document_id": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "profile": {"type": "string"},
            },
            ["document_id"],
        ),
    ),
    (
        "review_document_match",
        "Accept, reject or unlink a document match with version protection.",
        _schema(
            {
                "match_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject", "unmatch"]},
                "expected_version": {"type": "integer"},
            },
            ["match_id", "decision", "expected_version"],
        ),
    ),
    (
        "create_split",
        "Create a balanced split with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "profile": {"type": "string"},
                "expected_version": {"type": "integer"},
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "string"},
                            "category": {"type": ["string", "null"]},
                            "memo": {"type": ["string", "null"]},
                        },
                        "required": ["amount"],
                    },
                },
            },
            ["transaction_id", "profile", "expected_version", "parts"],
        ),
    ),
    (
        "update_split",
        "Replace a balanced split with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "profile": {"type": "string"},
                "expected_version": {"type": "integer"},
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "string"},
                            "category": {"type": ["string", "null"]},
                            "memo": {"type": ["string", "null"]},
                        },
                        "required": ["amount"],
                    },
                },
            },
            ["transaction_id", "profile", "expected_version", "parts"],
        ),
    ),
    (
        "revert_split",
        "Revert a split with version protection.",
        _schema(
            {"transaction_id": {"type": "string"}, "expected_version": {"type": "integer"}},
            ["transaction_id", "expected_version"],
        ),
    ),
    ("list_profiles", "List profiles, categories and rules.", _schema()),
    (
        "generate_report",
        "Generate local JSON/XLSX/HTML reports with explicit account scope.",
        _schema(
            {
                "profile": {"type": "string"},
                "extension": {"type": "string", "description": "Configured local extension name"},
                "formats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["json", "xlsx", "html"]},
                },
                "booking_from": {"type": "string"},
                "booking_to": {"type": "string"},
                "currency": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
            }
        ),
    ),
    (
        "list_report_runs",
        "List locally generated report runs for optional delivery.",
        _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    ),
    (
        "preview_report_delivery",
        "Create a masked delivery preview; sending through MCP is intentionally unavailable.",
        _schema(
            {
                "target": {"type": "string"},
                "report_run_id": {"type": "string"},
            },
            ["target"],
        ),
    ),
    (
        "create_manual_account",
        "Create a local manual account, asset or liability idempotently.",
        _schema(
            {
                "name": {"type": "string"},
                "account_type": {
                    "type": "string",
                    "enum": [
                        "checking",
                        "credit_card",
                        "savings",
                        "cash",
                        "loan",
                        "asset",
                        "liability",
                    ],
                },
                "currency": {"type": "string"},
                "opening_balance": {"type": "string"},
                "balance_date": {"type": "string"},
            },
            ["name", "account_type"],
        ),
    ),
    (
        "create_manual_transaction",
        "Create a local transaction only on manual accounts with stable idempotency and a review item.",
        _schema(
            {
                "account_id": {"type": "string"},
                "booking_date": {"type": "string"},
                "amount": {"type": "string"},
                "purpose": {"type": "string"},
                "valuta_date": {"type": "string"},
                "counterparty": {"type": "string"},
                "reference": {"type": "string"},
                "booking_status": {
                    "type": "string",
                    "enum": ["pending", "booked", "reversed", "corrected"],
                },
            },
            ["account_id", "booking_date", "amount", "purpose"],
        ),
    ),
    (
        "search_transactions",
        "Search transactions with combined account, text, amount, category, tag, dimension, merchant and document filters.",
        _schema(
            {
                "text": {"type": "string"},
                "booking_from": {"type": "string"},
                "booking_to": {"type": "string"},
                "amount_min": {"type": "string"},
                "amount_max": {"type": "string"},
                "currencies": {"type": "array", "items": {"type": "string"}},
                "booking_statuses": {"type": "array", "items": {"type": "string"}},
                "category_codes": {"type": "array", "items": {"type": "string"}},
                "classification_sources": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "dimensions": {
                    "type": "object",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                },
                "merchant": {"type": "string"},
                "document_status": {"type": "string", "enum": ["matched", "unmatched"]},
                "only_unreviewed": {"type": "boolean"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "minimum": 0},
            }
        ),
    ),
    (
        "create_tag",
        "Create a reusable transaction tag.",
        _schema({"name": {"type": "string"}}, ["name"]),
    ),
    ("list_tags", "List tags and usage counts.", _schema()),
    (
        "tag_transaction",
        "Add or remove a tag with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "tag": {"type": "string"},
                "operation": {"type": "string", "enum": ["add", "remove"]},
                "expected_version": {"type": "integer"},
            },
            ["transaction_id", "tag", "operation", "expected_version"],
        ),
    ),
    (
        "create_dimension",
        "Create a custom organization dimension.",
        _schema({"code": {"type": "string"}, "name": {"type": "string"}}, ["code", "name"]),
    ),
    (
        "create_dimension_value",
        "Create an organization dimension value.",
        _schema(
            {
                "dimension": {"type": "string"},
                "code": {"type": "string"},
                "name": {"type": "string"},
            },
            ["dimension", "code", "name"],
        ),
    ),
    ("list_dimensions", "List dimensions and values.", _schema()),
    (
        "set_transaction_dimension",
        "Set or remove a dimension value with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "dimension": {"type": "string"},
                "value": {"type": ["string", "null"]},
                "expected_version": {"type": "integer"},
            },
            ["transaction_id", "dimension", "expected_version"],
        ),
    ),
    (
        "create_merchant",
        "Create a normalized merchant.",
        _schema({"name": {"type": "string"}}, ["name"]),
    ),
    (
        "add_merchant_alias",
        "Link a local spelling to a merchant with provenance.",
        _schema(
            {"merchant": {"type": "string"}, "alias": {"type": "string"}}, ["merchant", "alias"]
        ),
    ),
    ("list_merchants", "List merchants, aliases and usage counts.", _schema()),
    (
        "set_transaction_merchant",
        "Set the normalized merchant with version protection.",
        _schema(
            {
                "transaction_id": {"type": "string"},
                "merchant": {"type": "string"},
                "expected_version": {"type": "integer"},
            },
            ["transaction_id", "merchant", "expected_version"],
        ),
    ),
    (
        "list_review_items",
        "List the account-specific review inbox.",
        _schema(
            {
                "status": {"type": "string", "enum": ["open", "resolved", "dismissed"]},
                "item_type": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_group": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "review_item",
        "Resolve or reject a review item with version protection.",
        _schema(
            {
                "review_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["resolve", "dismiss"]},
                "expected_version": {"type": "integer"},
            },
            ["review_id", "decision", "expected_version"],
        ),
    ),
    (
        "preview_bulk_change",
        "Store an atomic preview for at most 500 transactions bound to their versions.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "categorize",
                        "tag_add",
                        "tag_remove",
                        "dimension_set",
                        "dimension_clear",
                        "merchant_set",
                        "edit_fields",
                    ],
                },
                "transaction_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 500,
                },
                "payload": {"type": "object"},
            },
            ["action", "transaction_ids", "payload"],
        ),
    ),
    (
        "run_bulk_change",
        "Execute only an unchanged bulk preview atomically.",
        _schema(
            {
                "preview_id": {"type": "string"},
                "fingerprint": {"type": "string"},
            },
            ["preview_id"],
        ),
    ),
    (
        "create_saved_analysis",
        "Store a versioned analysis definition with account scope, filters, period rules and output formats.",
        _schema(
            {
                "code": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "definition": {"type": "object"},
            },
            ["code", "name", "definition"],
        ),
    ),
    (
        "update_saved_analysis",
        "Replace an analysis definition with version protection.",
        _schema(
            {
                "analysis": {"type": "string"},
                "definition": {"type": "object"},
                "expected_version": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            ["analysis", "definition", "expected_version"],
        ),
    ),
    ("list_saved_analyses", "List local saved analyses and their versions.", _schema()),
    (
        "get_saved_analysis",
        "Read a saved analysis definition.",
        _schema({"analysis": {"type": "string"}}, ["analysis"]),
    ),
    (
        "add_exchange_rate",
        "Store a dated versioned exchange rate idempotently.",
        _schema(
            {
                "source_currency": {"type": "string"},
                "target_currency": {"type": "string"},
                "rate": {"type": "string"},
                "rate_date": {"type": "string"},
                "source": {"type": "string"},
                "source_version": {"type": "string"},
            },
            ["source_currency", "target_currency", "rate", "rate_date", "source", "source_version"],
        ),
    ),
    (
        "list_exchange_rates",
        "List bounded local exchange-rate snapshots.",
        _schema(
            {
                "source_currency": {"type": "string"},
                "target_currency": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    ),
    (
        "run_saved_analysis",
        "Run a saved analysis and generate equivalent JSON/XLSX/HTML artifacts.",
        _schema(
            {
                "analysis": {"type": "string"},
                "as_of": {"type": "string"},
                "formats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["json", "xlsx", "html"]},
                },
            },
            ["analysis"],
        ),
    ),
    (
        "export_portable_data",
        "Create a versioned checksummed local full export.",
        _schema({"output": {"type": "string"}}, ["output"]),
    ),
    (
        "create_encrypted_backup",
        "Create an encrypted local full backup; read the key only through a secret reference.",
        _schema(
            {"output": {"type": "string"}, "encryption_secret": {"type": "string"}},
            ["output", "encryption_secret"],
        ),
    ),
    (
        "verify_portable_artifact",
        "Check export or backup checksums, manifest and SQLite integrity.",
        _schema({"path": {"type": "string"}, "encryption_secret": {"type": "string"}}, ["path"]),
    ),
    (
        "check_data_integrity",
        "Check local SQLite, foreign key and artifact integrity.",
        _schema(),
    ),
    (
        "preview_data_deletion",
        "Create a fingerprint-bound deletion preview; execution remains local through the CLI.",
        _schema(
            {
                "scopes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["database", "documents", "reports", "backups", "exports"],
                    },
                    "minItems": 1,
                }
            },
            ["scopes"],
        ),
    ),
)

MUTATING_TOOLS = {
    "preview_account_import",
    "import_account_file",
    "sync_comdirect",
    "run_classification",
    "preview_llm_classification",
    "execute_llm_classification_preview",
    "review_classification",
    "classify_transaction",
    "add_rule",
    "add_hint",
    "import_document",
    "run_document_batch",
    "apply_document_match_preview",
    "match_document",
    "review_document_match",
    "create_split",
    "update_split",
    "revert_split",
    "generate_report",
    "preview_report_delivery",
    "transition_transaction_status",
    "suggest_transaction_relations",
    "create_transaction_relation",
    "review_transaction_relation",
    "record_balance_snapshot",
    "reconcile_balance_snapshot",
    "create_manual_account",
    "create_manual_transaction",
    "create_tag",
    "tag_transaction",
    "create_dimension",
    "create_dimension_value",
    "set_transaction_dimension",
    "create_merchant",
    "add_merchant_alias",
    "set_transaction_merchant",
    "review_item",
    "preview_bulk_change",
    "run_bulk_change",
    "create_saved_analysis",
    "update_saved_analysis",
    "add_exchange_rate",
    "run_saved_analysis",
    "export_portable_data",
    "create_encrypted_backup",
    "preview_data_deletion",
}


class MCPService:
    def __init__(
        self,
        config_path: str | None = None,
        db_path: str | None = None,
        *,
        workspace_id: str | None = None,
        registry_path: str | None = None,
    ) -> None:
        self.config_path = config_path
        self.db_path = db_path
        self.workspace_id = workspace_id
        self.registry = WorkspaceRegistry(registry_path)
        self._repository: LedgerRepository | None = None
        self._secrets: Any = None

    @property
    def loaded(self) -> Any:
        return resolve_workspace(
            self.workspace_id, self.config_path, self.db_path, registry=self.registry
        )

    @property
    def repository(self) -> LedgerRepository:
        if self._repository is None:
            self._repository = LedgerRepository(LedgerDatabase(self.loaded.config.database.path))
            install_builtin_profiles(self._repository)
        return self._repository

    @property
    def secrets(self) -> Any:
        if self._secrets is None:
            with workspace_operation(self.loaded, self.registry):
                self._secrets = SecretStore(dotenv_path=self.loaded.project_path.with_name(".env"))
        return self._secrets

    @secrets.setter
    def secrets(self, value: Any) -> None:
        self._secrets = value

    def _scope(self, arguments: dict[str, Any]) -> AccountScope:
        accounts = arguments.get("account_ids")
        group = arguments.get("account_group")
        if accounts and group:
            raise ValueError("Use either account_ids or account_group")
        if group:
            return self.repository.account_scope_for_group(group)
        return AccountScope.selected(accounts) if accounts else AccountScope.all()

    def _matcher(self) -> DocumentMatcher:
        config = self.loaded.config.documents
        return DocumentMatcher(
            self.repository,
            config.storage_dir,
            auto_match_threshold=config.auto_match_threshold,
            auto_match_minimum_gap=config.auto_match_minimum_gap,
            extract_pdf_text=config.extract_pdf_text,
        )

    def _document_batches(self) -> DocumentBatchService:
        config = self.loaded.config.documents
        return DocumentBatchService(
            self.repository,
            config.storage_dir,
            max_file_size_bytes=self.loaded.config.imports.max_file_size_mib * 1024 * 1024,
            ocr_provider=create_local_ocr_provider(
                enabled=config.local_ocr_enabled,
                executable=config.local_ocr_executable,
                pdf_renderer=config.local_ocr_pdf_renderer,
                languages=config.local_ocr_languages,
                timeout_seconds=config.local_ocr_timeout_seconds,
            ),
        )

    def _portability(self) -> PortabilityService:
        return PortabilityService(
            self.loaded.config.database.path,
            self.loaded.config.documents.storage_dir,
            self.loaded.config.reporting.output_dir,
            self.loaded.project_path,
            masked_config=masked_config(self.loaded.config),
        )

    def _semantics(self) -> TransactionSemanticsService:
        config = self.loaded.config.transaction_semantics
        return TransactionSemanticsService(
            self.repository,
            transfer_max_days=config.transfer_max_days,
            transfer_max_fee_minor=config.transfer_max_fee_minor,
            refund_max_days=config.refund_max_days,
            correction_max_days=config.correction_max_days,
            minimum_score=config.minimum_candidate_score,
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_workspaces":
            return {"workspaces": self.registry.list()}
        selected = arguments.get("workspace_id")
        if selected:
            if self.config_path or self.workspace_id:
                if str(self.loaded.config.workspace.id) != selected:
                    raise ValueError("Tool workspace differs from the server binding")
            else:
                service = MCPService(
                    workspace_id=str(selected),
                    db_path=self.db_path,
                    registry_path=str(self.registry.path),
                )
                return service.execute(
                    name, {key: value for key, value in arguments.items() if key != "workspace_id"}
                )
        if name == "workspace_status":
            from dagoberd.workspace_status import workspace_status

            return workspace_status(self.registry, str(self.loaded.config.workspace.id))
        if name in {"workspace_sync_status", "workspace_sync_preview"}:
            from dagoberd.workspace_sync import WorkspaceSync

            sync = WorkspaceSync(self.registry, str(self.loaded.config.workspace.id))
            if name == "workspace_sync_status":
                return sync.status()
            if arguments.get("action") == "publish":
                return sync.preview("publish")
            if arguments.get("action") == "restore":
                return sync.preview("restore")
            raise ValueError("Unknown snapshot action")
        if name in {"workspace_git_status", "workspace_git_preview"}:
            from dagoberd.workspace_git import WorkspaceGit

            service_git = WorkspaceGit(self.registry, str(self.loaded.config.workspace.id))
            if name == "workspace_git_status":
                return service_git.status()
            return service_git.preview(
                str(arguments["action"]), arguments.get("files"), str(arguments.get("message", ""))
            )
        with workspace_operation(self.loaded, self.registry):
            if output := arguments.get("output"):
                self.registry.validate_output(self.loaded, output)
            return self._execute_scoped(name, arguments)

    def _execute_scoped(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in MUTATING_TOOLS:
            return self._execute_once(name, arguments)
        idempotency_key = str(arguments.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise ValueError("Mutating MCP tools require idempotency_key")
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                """SELECT tool_name, result_json FROM ledger_mcp_requests
                   WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
        if existing:
            if existing["tool_name"] != name:
                raise ValueError("idempotency_key was already used for another tool")
            return json.loads(existing["result_json"])
        result = self._execute_once(name, arguments)
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_mcp_requests(
                       idempotency_key, tool_name, result_json
                   ) VALUES (?, ?, ?)""",
                (idempotency_key, name, json.dumps(result, default=str)),
            )
            self.repository._audit(
                connection,
                "mcp_request",
                idempotency_key,
                name,
                None,
                {"status": "completed"},
                actor="mcp",
                correlation_id=idempotency_key,
                idempotency_key=f"mcp:{idempotency_key}",
            )
        return result

    def _execute_once(self, name: str, arguments: dict[str, Any]) -> Any:
        repo = self.repository
        organization = OrganizationService(repo)
        analysis = AnalysisService(repo)
        if name == "get_status":
            return {
                "diagnostics": [
                    item.as_dict() for item in run_diagnostics(self.loaded, self.secrets)
                ],
                "configuration": masked_config(self.loaded.config),
            }
        if name == "system_status":
            return {
                "diagnostics": [
                    item.as_dict() for item in run_diagnostics(self.loaded, self.secrets)
                ],
                "versions": versions(),
            }
        if name == "contract_versions":
            return versions()
        if name == "config_summary":
            return {"configuration": masked_config(self.loaded.config)}
        if name == "list_accounts":
            return {
                "accounts": [
                    safe_financial_record(item)
                    for item in repo.list_accounts(enabled_only=bool(arguments.get("enabled_only")))
                ]
            }
        if name == "list_transactions":
            requested_statuses = arguments.get("booking_statuses")
            rows = repo.list_transactions(
                int(arguments.get("limit", 50)),
                arguments.get("booking_from"),
                arguments.get("booking_to"),
                arguments.get("profile"),
                self._scope(arguments),
                tuple(requested_statuses)
                if requested_statuses is not None
                else ("pending", "booked"),
            )
            return {"transactions": [safe_financial_record(item) for item in rows]}
        if name == "create_manual_account":
            return safe_financial_record(
                organization.create_manual_account(
                    arguments["name"],
                    account_kind=arguments["account_type"],
                    currency=arguments.get("currency", "EUR"),
                    idempotency_key=arguments["idempotency_key"],
                    opening_balance=(
                        Decimal(arguments["opening_balance"])
                        if arguments.get("opening_balance") is not None
                        else None
                    ),
                    balance_date=(
                        date.fromisoformat(arguments["balance_date"])
                        if arguments.get("balance_date")
                        else None
                    ),
                )
            )
        if name == "create_manual_transaction":
            if not self.loaded.config.agents.allow_manual_transactions_mcp:
                raise ValueError(
                    "Manual transaction creation via MCP is disabled; "
                    "use the CLI or set agents.allow_manual_transactions_mcp = true"
                )
            return safe_financial_record(
                organization.create_manual_transaction(
                    arguments["account_id"],
                    booking_date=date.fromisoformat(arguments["booking_date"]),
                    valuta_date=(
                        date.fromisoformat(arguments["valuta_date"])
                        if arguments.get("valuta_date")
                        else None
                    ),
                    amount=Decimal(arguments["amount"]),
                    purpose=arguments["purpose"],
                    counterparty=arguments.get("counterparty"),
                    reference=arguments.get("reference"),
                    booking_status=arguments.get("booking_status", "booked"),
                    idempotency_key=arguments["idempotency_key"],
                )
            )
        if name == "search_transactions":
            rows = organization.search_transactions(
                TransactionSearchFilter(
                    text=arguments.get("text"),
                    booking_from=arguments.get("booking_from"),
                    booking_to=arguments.get("booking_to"),
                    amount_min_minor=(
                        to_minor_units(Decimal(arguments["amount_min"]))
                        if arguments.get("amount_min") is not None
                        else None
                    ),
                    amount_max_minor=(
                        to_minor_units(Decimal(arguments["amount_max"]))
                        if arguments.get("amount_max") is not None
                        else None
                    ),
                    currencies=tuple(arguments.get("currencies") or ()),
                    booking_statuses=tuple(arguments.get("booking_statuses") or ()),
                    category_codes=tuple(arguments.get("category_codes") or ()),
                    classification_sources=tuple(arguments.get("classification_sources") or ()),
                    tags=tuple(arguments.get("tags") or ()),
                    dimensions={
                        str(code): tuple(values)
                        for code, values in (arguments.get("dimensions") or {}).items()
                    },
                    merchant=arguments.get("merchant"),
                    document_status=arguments.get("document_status"),
                    only_unreviewed=bool(arguments.get("only_unreviewed")),
                ),
                account_scope=self._scope(arguments),
                limit=min(int(arguments.get("limit", 100)), 500),
                offset=int(arguments.get("offset", 0)),
            )
            return {"transactions": [safe_financial_record(row) for row in rows]}
        if name == "create_tag":
            return organization.create_tag(arguments["name"])
        if name == "list_tags":
            return {"tags": organization.list_tags()}
        if name == "tag_transaction":
            return organization.tag_transaction(
                arguments["transaction_id"],
                arguments["tag"],
                expected_version=int(arguments["expected_version"]),
                remove=arguments["operation"] == "remove",
            )
        if name == "create_dimension":
            return organization.create_dimension(arguments["code"], arguments["name"])
        if name == "create_dimension_value":
            return organization.create_dimension_value(
                arguments["dimension"], arguments["code"], arguments["name"]
            )
        if name == "list_dimensions":
            return {"dimensions": organization.list_dimensions()}
        if name == "set_transaction_dimension":
            return organization.set_transaction_dimension(
                arguments["transaction_id"],
                arguments["dimension"],
                arguments.get("value"),
                expected_version=int(arguments["expected_version"]),
            )
        if name == "create_merchant":
            return organization.create_merchant(arguments["name"])
        if name == "add_merchant_alias":
            return organization.add_merchant_alias(arguments["merchant"], arguments["alias"])
        if name == "list_merchants":
            return {"merchants": organization.list_merchants()}
        if name == "set_transaction_merchant":
            return organization.set_transaction_merchant(
                arguments["transaction_id"],
                arguments["merchant"],
                expected_version=int(arguments["expected_version"]),
            )
        if name == "list_review_items":
            return {
                "items": organization.list_review_items(
                    status=arguments.get("status", "open"),
                    item_type=arguments.get("item_type"),
                    account_scope=self._scope(arguments),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "review_item":
            return organization.review_item(
                arguments["review_id"],
                arguments["decision"],
                expected_version=int(arguments["expected_version"]),
            )
        if name == "preview_bulk_change":
            return organization.create_bulk_preview(
                arguments["action"],
                arguments["transaction_ids"],
                arguments["payload"],
            )
        if name == "run_bulk_change":
            return organization.execute_bulk_preview(
                arguments["preview_id"], fingerprint=arguments.get("fingerprint")
            )
        if name == "create_saved_analysis":
            return analysis.create_saved_analysis(
                arguments["code"],
                arguments["name"],
                AnalysisDefinition.model_validate(arguments["definition"]),
                description=arguments.get("description", ""),
            )
        if name == "update_saved_analysis":
            return analysis.update_saved_analysis(
                arguments["analysis"],
                AnalysisDefinition.model_validate(arguments["definition"]),
                expected_version=int(arguments["expected_version"]),
                name=arguments.get("name"),
                description=arguments.get("description"),
            )
        if name == "list_saved_analyses":
            return {"analyses": analysis.list_saved_analyses()}
        if name == "get_saved_analysis":
            return analysis.get_saved_analysis(arguments["analysis"])
        if name == "add_exchange_rate":
            return analysis.add_exchange_rate(
                arguments["source_currency"],
                arguments["target_currency"],
                Decimal(arguments["rate"]),
                rate_date=date.fromisoformat(arguments["rate_date"]),
                source=arguments["source"],
                source_version=arguments["source_version"],
                idempotency_key=arguments["idempotency_key"],
            )
        if name == "list_exchange_rates":
            return {
                "rates": analysis.list_exchange_rates(
                    source_currency=arguments.get("source_currency"),
                    target_currency=arguments.get("target_currency"),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "run_saved_analysis":
            return analysis.run_saved_analysis(
                arguments["analysis"],
                self.loaded.config.reporting.output_dir,
                as_of=(date.fromisoformat(arguments["as_of"]) if arguments.get("as_of") else None),
                formats=(tuple(arguments["formats"]) if arguments.get("formats") else None),
            )
        if name == "list_transaction_history":
            return {"events": repo.list_transaction_status_history(arguments["transaction_id"])}
        if name == "transition_transaction_status":
            version = repo.transition_transaction_status(
                arguments["transaction_id"],
                arguments["booking_status"],
                int(arguments["expected_version"]),
                reason=arguments["reason"],
                source="mcp",
            )
            return {
                "transaction_id": arguments["transaction_id"],
                "booking_status": arguments["booking_status"],
                "version": version,
            }
        if name == "suggest_transaction_relations":
            return {
                "relations": self._semantics().suggest(
                    arguments["relation_type"],
                    transaction_id=arguments.get("transaction_id"),
                    account_scope=self._scope(arguments),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "list_transaction_relations":
            return {
                "relations": repo.list_transaction_relations(
                    relation_type=arguments.get("relation_type"),
                    status=arguments.get("status"),
                    transaction_id=arguments.get("transaction_id"),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "create_transaction_relation":
            return repo.create_transaction_relation(
                arguments["relation_type"],
                arguments["source_transaction_id"],
                arguments["target_transaction_id"],
                expected_source_version=int(arguments["expected_source_version"]),
                expected_target_version=int(arguments["expected_target_version"]),
                fee_amount_minor=to_minor_units(Decimal(arguments.get("fee", "0"))),
                reasons=arguments.get("reasons") or [],
                metadata={"created_by": "mcp"},
            )
        if name == "review_transaction_relation":
            return repo.review_transaction_relation(
                arguments["relation_id"],
                arguments["decision"],
                int(arguments["expected_version"]),
            )
        if name == "record_balance_snapshot":
            result = repo.record_balance_snapshot(
                arguments["account_id"],
                to_minor_units(Decimal(arguments["amount"])),
                balance_date=arguments["balance_date"],
                source=arguments.get("source", "mcp"),
                external_id=arguments.get("external_id"),
                expected_account_version=int(arguments["expected_version"]),
            )
            return safe_financial_record(result)
        if name == "list_balance_snapshots":
            return {
                "snapshots": [
                    safe_financial_record(item)
                    for item in repo.list_balance_snapshots(
                        account_id=arguments.get("account_id"),
                        limit=min(int(arguments.get("limit", 100)), 500),
                    )
                ]
            }
        if name == "reconcile_balance_snapshot":
            return repo.reconcile_balance_snapshot(
                arguments["snapshot_id"],
                refresh=True,
            )
        if name == "list_reconciliations":
            return {
                "reconciliations": repo.list_reconciliations(
                    account_id=arguments.get("account_id"),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "list_account_import_history":
            return {
                "imports": ImportService(repo).history(
                    arguments["account_id"], int(arguments.get("limit", 100))
                )
            }
        if name == "preview_account_import":
            imports = self.loaded.config.imports
            return (
                ImportService(
                    repo,
                    imports.mappings,
                    imports.profiles,
                    imports.max_file_size_mib,
                )
                .preview(
                    arguments["source"],
                    arguments["path"],
                    arguments.get("account_id"),
                    arguments.get("profile"),
                )
                .as_dict()
            )
        if name == "import_account_file":
            imports = self.loaded.config.imports
            return (
                ImportService(
                    repo,
                    imports.mappings,
                    imports.profiles,
                    imports.max_file_size_mib,
                )
                .execute(arguments["preview_id"])
                .as_dict()
            )
        if name == "diagnose_comdirect":
            connection_name = arguments["connection"]
            return ComdirectDiagnosticsService(
                repo,
                connection_name,
                self.loaded.config.comdirect.connection(connection_name),
                self.secrets,
            ).diagnose()
        if name == "sync_comdirect":
            connection_name = arguments["connection"]
            config = self.loaded.config.comdirect.connection(connection_name)
            if not (
                config.enabled and config.session_mode == "keychain_timed" and config.allow_mcp_sync
            ):
                raise ValueError("Comdirect MCP sync is not locally enabled")
            serialized = self.secrets.get_keyring(comdirect_timed_session_secret(connection_name))
            if not serialized:
                raise ValueError(
                    f"Run 'dagoberd auth comdirect {connection_name}' in a local terminal first"
                )
            state = ComdirectSessionState.from_json(serialized)
            if not state.is_within(config.keychain_session_max_age_hours):
                expired_session = ComdirectProvider(timeout_seconds=config.timeout_seconds)
                expired_session.restore_session(state.access_only())
                try:
                    expired_session.revoke()
                finally:
                    self.secrets.delete(comdirect_timed_session_secret(connection_name))
                raise ValueError("Die lokal freigegebene Comdirect-Session ist abgelaufen")
            provider = ComdirectProvider(timeout_seconds=config.timeout_seconds)
            provider.restore_session(state.access_only())
            provider.allow_token_refresh = False
            sync_result = ComdirectSyncService(repo, provider, connection_name).synchronize(
                account_ids=arguments.get("account_ids"),
                booking_from=date.fromisoformat(arguments["booking_from"])
                if arguments.get("booking_from")
                else None,
                booking_to=date.fromisoformat(arguments["booking_to"])
                if arguments.get("booking_to")
                else None,
                resume_sync_run_id=arguments.get("resume_sync_run_id"),
                interactive=False,
            )
            return asdict(sync_result)
        if name == "run_classification":
            if self.loaded.config.llm.enabled and self.loaded.config.llm.mode == "cloud":
                raise ValueError(
                    "Cloud LLM use requires preview_llm_classification followed by "
                    "execute_llm_classification_preview"
                )
            service = ClassificationService(
                repo,
                create_llm_provider(self.loaded.config, self.secrets),
                self.loaded.config.privacy,
            )
            if arguments.get("transaction_id"):
                return {
                    "classification_id": service.classify(
                        arguments["transaction_id"], arguments["profile"]
                    )
                }
            return {
                "results": service.classify_open(
                    arguments["profile"],
                    limit=min(int(arguments.get("limit", 100)), 500),
                    account_scope=self._scope(arguments),
                )
            }
        if name == "preview_llm_classification":
            return LLMPreviewService(
                repo,
                None,
                self.loaded.config.llm,
                self.loaded.config.privacy,
            ).create(
                arguments["profile"],
                transaction_ids=arguments.get("transaction_ids"),
                account_scope=self._scope(arguments),
                limit=min(int(arguments.get("limit", 100)), 100),
            )
        if name == "execute_llm_classification_preview":
            return LLMPreviewService(
                repo,
                create_llm_provider(self.loaded.config, self.secrets),
                self.loaded.config.llm,
                self.loaded.config.privacy,
            ).execute(arguments["preview_id"], arguments["fingerprint"])
        if name == "list_classification_suggestions":
            return {
                "suggestions": repo.list_classifications(
                    status="suggested",
                    profile_code=arguments.get("profile"),
                    limit=min(int(arguments.get("limit", 100)), 500),
                )
            }
        if name == "review_classification":
            return repo.review_classification(
                arguments["classification_id"],
                arguments["decision"],
                int(arguments["expected_version"]),
            )
        if name == "classify_transaction":
            return {
                "classification_id": repo.classify(
                    arguments["transaction_id"],
                    arguments["profile"],
                    arguments["category"],
                    "manual",
                    1.0,
                    arguments.get("rationale", "MCP"),
                    expected_version=int(arguments["expected_version"]),
                )
            }
        if name == "add_rule":
            return {
                "rule_id": repo.add_rule(
                    arguments["profile"],
                    arguments["name"],
                    arguments["pattern"],
                    arguments["category"],
                    int(arguments.get("priority", 100)),
                )
            }
        if name == "add_hint":
            return {
                "hint_id": repo.add_hint(
                    arguments["profile"],
                    arguments["text"],
                    pattern=arguments.get("pattern"),
                    category_code=arguments.get("category"),
                    priority=int(arguments.get("priority", 100)),
                )
            }
        if name == "import_document":
            return self._matcher().import_file(
                arguments["path"],
                amount=Decimal(arguments["amount"]) if arguments.get("amount") else None,
                document_date=date.fromisoformat(arguments["date"])
                if arguments.get("date")
                else None,
                merchant=arguments.get("merchant"),
                reference=arguments.get("reference"),
                account_id=arguments.get("account_id"),
                profile_code=arguments.get("profile"),
            )
        if name == "list_documents":
            documents = self._matcher().list_documents(status=arguments.get("status"))
            for document in documents:
                document.pop("storage_path", None)
                document.pop("content_hash", None)
            return {"documents": documents}
        if name == "preview_document_batch":
            return self._document_batches().preview(arguments["path"]).as_dict()
        if name == "run_document_batch":
            return (
                self._document_batches()
                .execute(arguments["preview_id"], arguments["fingerprint"])
                .as_dict()
            )
        if name == "preview_document_matches":
            return self._matcher().preview_matches(
                arguments["document_id"],
                account_scope=self._scope(arguments),
                profile_code=arguments.get("profile"),
                limit=min(int(arguments.get("limit", 100)), 500),
            )
        if name == "apply_document_match_preview":
            return self._matcher().apply_match_preview(
                arguments["document_id"],
                arguments["fingerprint"],
                list(arguments["transaction_ids"]),
                expected_document_version=int(arguments["expected_document_version"]),
                append=bool(arguments.get("append", True)),
            )
        if name == "list_matches":
            clauses = []
            parameters: list[object] = []
            if arguments.get("status"):
                clauses.append("dm.status=?")
                parameters.append(arguments["status"])
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            parameters.append(min(int(arguments.get("limit", 100)), 500))
            with repo.database.connect() as connection:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT dm.id, dm.document_id, dm.transaction_id,
                              dm.score, dm.reasons_json, dm.status, dm.version,
                              dm.created_at, dm.updated_at
                       FROM ledger_document_matches dm"""
                        + where
                        + " ORDER BY dm.created_at DESC LIMIT ?",
                        parameters,
                    )
                ]
            return {"matches": rows}
        if name == "match_document":
            return {
                "candidates": self._matcher().suggest(
                    arguments["document_id"],
                    account_scope=self._scope(arguments),
                    profile_code=arguments.get("profile"),
                )
            }
        if name == "review_document_match":
            matcher = self._matcher()
            if arguments["decision"] == "unmatch":
                return matcher.unmatch(arguments["match_id"], int(arguments["expected_version"]))
            return matcher.review(
                arguments["match_id"], arguments["decision"], int(arguments["expected_version"])
            )
        if name == "create_split":
            parts: list[tuple[object, str | None, str | None]] = [
                (
                    Decimal(part["amount"]),
                    str(part["category"]) if part.get("category") is not None else None,
                    str(part["memo"]) if part.get("memo") is not None else None,
                )
                for part in arguments["parts"]
            ]
            return {
                "split_ids": repo.split_transaction(
                    arguments["transaction_id"],
                    arguments["profile"],
                    parts,
                    int(arguments["expected_version"]),
                )
            }
        if name == "update_split":
            parts = [
                (
                    Decimal(part["amount"]),
                    str(part["category"]) if part.get("category") is not None else None,
                    str(part["memo"]) if part.get("memo") is not None else None,
                )
                for part in arguments["parts"]
            ]
            return {
                "split_ids": repo.replace_split(
                    arguments["transaction_id"],
                    arguments["profile"],
                    parts,
                    int(arguments["expected_version"]),
                )
            }
        if name == "revert_split":
            repo.revert_split(arguments["transaction_id"], int(arguments["expected_version"]))
            return {"transaction_id": arguments["transaction_id"], "status": "active"}
        if name == "list_profiles":
            with repo.database.connect() as connection:
                return {
                    "profiles": [
                        dict(row)
                        for row in connection.execute(
                            """SELECT p.code, p.name, p.description, c.code AS category_code, c.name AS category_name FROM ledger_profiles p LEFT JOIN ledger_profile_categories pc ON pc.profile_id=p.id LEFT JOIN ledger_categories c ON c.id=pc.category_id ORDER BY p.code, c.code"""
                        )
                    ]
                }
        if name == "generate_report":
            paths = LedgerReportGenerator(
                repo,
                self.loaded.config.reporting.output_dir,
                extensions=self.loaded.config.reporting.extensions,
            ).generate(
                arguments.get("profile"),
                tuple(arguments.get("formats") or self.loaded.config.reporting.default_formats),
                account_scope=self._scope(arguments),
                booking_from=arguments.get("booking_from"),
                booking_to=arguments.get("booking_to"),
                currency=arguments.get("currency"),
                extension=arguments.get("extension"),
            )
            return {"paths": [str(path) for path in paths]}
        if name == "list_report_runs":
            return {
                "report_runs": ReportDeliveryService(
                    repo,
                    self.loaded.config.delivery,
                    self.secrets,
                ).list_report_runs(min(int(arguments.get("limit", 20)), 100))
            }
        if name == "preview_report_delivery":
            return ReportDeliveryService(
                repo,
                self.loaded.config.delivery,
                self.secrets,
            ).preview(
                arguments["target"],
                arguments.get("report_run_id", "latest"),
            )
        if name == "export_portable_data":
            return self._portability().export(arguments["output"])
        if name == "create_encrypted_backup":
            password = self.secrets.get(arguments["encryption_secret"])
            if not password:
                raise ValueError("Configured backup password secret is missing")
            return self._portability().encrypted_backup(
                arguments["output"],
                password,
            )
        if name == "verify_portable_artifact":
            password = None
            if arguments.get("encryption_secret"):
                password = self.secrets.get(arguments["encryption_secret"])
                if not password:
                    raise ValueError("Configured backup password secret is missing")
            return self._portability().verify(
                arguments["path"],
                password=password,
            )
        if name == "check_data_integrity":
            return self._portability().integrity()
        if name == "preview_data_deletion":
            return self._portability().preview_deletion(arguments["scopes"]).as_dict()
        raise KeyError(f"Unknown tool: {name}")


def _content(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]


def create_server(
    db_path: str | None = None,
    config_path: str | None = None,
    *,
    workspace_id: str | None = None,
    registry_path: str | None = None,
) -> Server:
    if Server is None:
        raise RuntimeError("The 'mcp' package is missing. Install the project dependencies.")
    server = Server("dagoberd", version=versions()["product_version"])

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        registry = WorkspaceRegistry(registry_path)
        entries = registry.read().workspaces
        configs = (
            [registry.load(entry.id).config for entry in entries]
            if entries and not config_path and not workspace_id
            else [resolve_workspace(workspace_id, config_path, db_path, registry=registry).config]
        )
        tools = []
        for name, description, schema in TOOL_DEFINITIONS:
            if name == "sync_comdirect" and not any(
                connection.enabled
                and connection.session_mode == "keychain_timed"
                and connection.allow_mcp_sync
                for config in configs
                for connection in config.comdirect.connections.values()
            ):
                continue
            rendered = json.loads(json.dumps(schema))
            rendered["properties"]["workspace_id"] = {
                "type": "string",
                "description": "Workspace UUID",
            }
            if len(entries) > 1 and not config_path and not workspace_id:
                rendered.setdefault("required", []).append("workspace_id")
            if name in MUTATING_TOOLS:
                rendered["properties"]["idempotency_key"] = {
                    "type": "string",
                    "minLength": 8,
                    "description": "Stable client-selected key for retries.",
                }
                rendered.setdefault("required", []).append("idempotency_key")
            tools.append(Tool(name=name, description=description, inputSchema=rendered))
        tools.append(
            Tool(
                name="list_workspaces",
                description="List locally registered workspaces",
                inputSchema={"type": "object", "properties": {}},
            )
        )
        for name in (
            "workspace_status",
            "workspace_sync_status",
            "workspace_sync_preview",
            "workspace_git_status",
            "workspace_git_preview",
        ):
            tools.append(
                Tool(
                    name=name,
                    description="Inspect local workspace capabilities or prepare an optional exchange; execution stays in the local CLI/GUI",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "workspace_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["publish", "restore"]
                                if name == "workspace_sync_preview"
                                else ["pull", "commit", "push"],
                            },
                            "files": {"type": "array", "items": {"type": "string"}},
                            "message": {"type": "string"},
                        },
                    },
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            return _content(
                MCPService(
                    config_path, db_path, workspace_id=workspace_id, registry_path=registry_path
                ).execute(name, arguments)
            )
        except VersionConflictError as error:
            return _content({"error": "version_conflict", "message": str(error)})
        except LLMProviderError as error:
            return _content({"error": "llm_failed", "message": str(error)})
        except (FileNotFoundError, KeyError, ValueError) as error:
            return _content({"error": "invalid_request", "message": str(error)})
        except Exception as error:  # keep stdio protocol alive; never expose secrets
            return _content({"error": "operation_failed", "message": type(error).__name__})

    return server


async def run() -> None:
    server = create_server(
        db_path=os.environ.get("ACCOUNT_EVAL_DB"),
        config_path=os.environ.get("ACCOUNT_EVAL_CONFIG"),
        workspace_id=os.environ.get("ACCOUNT_EVAL_WORKSPACE"),
        registry_path=os.environ.get("ACCOUNT_EVAL_WORKSPACE_REGISTRY"),
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
