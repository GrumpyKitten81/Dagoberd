"""Local command line interface for Dagoberd."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from dagoberd.accounts import AccountScope
from dagoberd.config import (
    ComdirectConnectionConfig,
    LoadedConfig,
    comdirect_timed_session_secret,
    initialize_project_config,
    load_config,
    masked_config,
)
from dagoberd.contracts import ExitCode, failure, success, versions
from dagoberd.doctor import run_diagnostics
from dagoberd.documents.batch import DocumentBatchService
from dagoberd.documents.extraction import create_local_ocr_provider
from dagoberd.documents.matching import DocumentMatcher
from dagoberd.importers import available_sources
from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import TransactionSearchFilter, to_minor_units
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.pipeline import PipelineConfig
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError
from dagoberd.ledger.semantics import TransactionSemanticsService
from dagoberd.llm import create_llm_provider
from dagoberd.llm.openai_compatible import LLMProviderError
from dagoberd.onboarding import (
    LLM_PRESETS,
    SetupAnswers,
    configure_agent_launcher,
    configure_local_project,
    create_demo_workspace,
    diagnose_agent_runtime,
    remove_demo_workspace,
    run_offline_self_test,
)
from dagoberd.privacy import safe_financial_record
from dagoberd.profiles import install_builtin_profiles
from dagoberd.providers.comdirect import (
    ComdirectProvider,
    ComdirectSessionState,
)
from dagoberd.providers.comdirect_cli import LocalCLIChallengeHandler
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.secrets import SecretStore
from dagoberd.services.comdirect_diagnostics import ComdirectDiagnosticsService
from dagoberd.services.comdirect_sync import ComdirectSyncService, SyncResult
from dagoberd.services.import_service import ImportService
from dagoberd.services.llm_preview import LLMPreviewService
from dagoberd.services.portability import PortabilityService
from dagoberd.services.report_delivery import ReportDeliveryService
from dagoberd.services.restore_files import RestoreError
from dagoberd.services.source_reconciliation import SourceReconciliationService
from dagoberd.services.wiso_tree_taxonomy import (
    import_wiso_tree_taxonomy,
    preview_wiso_tree_taxonomy,
)
from dagoberd.workspace_cli import add_workspace_commands, handle_workspace_command
from dagoberd.workspaces import (
    CURRENT_WORKSPACE,
    WorkspaceRegistry,
    resolve_workspace,
    workspace_operation,
)


def repository(
    db_path: str | None,
    config_path: str | None = None,
) -> LedgerRepository:
    overrides = {"database": {"path": db_path}} if db_path else None
    loaded = CURRENT_WORKSPACE.get() or load_config(config_path, overrides=overrides)
    repo = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repo)
    return repo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dagoberd",
        description="Local financial ledger",
    )
    parser.add_argument("--config", help="Path to account-eval.toml")
    parser.add_argument("--workspace", help="Registered workspace name or UUID")
    parser.add_argument("--workspace-registry", help="Path to the local workspace registry")
    parser.add_argument("--db", help="Path to the local SQLite database")
    parser.add_argument(
        "--output",
        dest="render_mode",
        choices=("human", "json", "legacy-json"),
        default=None,
        help="Output format; legacy-json preserves existing script contracts",
    )
    parser.add_argument("--json", dest="render_mode", action="store_const", const="json")
    commands = parser.add_subparsers(dest="command", required=True)
    add_workspace_commands(commands)

    init = commands.add_parser("init", help="Initialize project configuration and ledger")
    init.add_argument("--force", action="store_true")

    setup = commands.add_parser("setup", help="Set up the local installation")
    setup.add_argument("--comdirect", action=argparse.BooleanOptionalAction, default=False)
    setup.add_argument("--llm-preset", choices=tuple(LLM_PRESETS), default="disabled")
    setup.add_argument("--llm-model", default="")
    setup.add_argument("--llm-base-url")
    setup.add_argument("--base-currency", default="EUR")
    setup.add_argument("--yes", action="store_true")

    demo = commands.add_parser("demo", help="Manage a synthetic demo workspace")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_create = demo_commands.add_parser("create")
    demo_create.add_argument("--path", default=".account-eval/demo")
    demo_create.add_argument("--force", action="store_true")
    demo_reset = demo_commands.add_parser("reset")
    demo_reset.add_argument("--path", default=".account-eval/demo")
    demo_reset.add_argument("--yes", action="store_true", required=True)

    agents = commands.add_parser("agents", help="Configure and check optional agent integration")
    agent_commands = agents.add_subparsers(dest="agents_command", required=True)
    agent_configure = agent_commands.add_parser("configure")
    agent_configure.add_argument("--workspace", dest="agent_workspace")
    agent_doctor = agent_commands.add_parser(
        "doctor", help="Check installation, configuration and ledger"
    )
    agent_doctor.add_argument("--workspace", dest="agent_workspace")

    commands.add_parser("self-test", help="Check the installation offline with synthetic data")
    commands.add_parser("contracts", help="Show interface versions")
    completion = commands.add_parser("completion", help="Generate shell completion")
    completion.add_argument("shell", choices=("bash", "zsh", "fish", "powershell"))

    config = commands.add_parser("config", help="Manage configuration and local secrets")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("validate")
    config_show = config_commands.add_parser("show")
    config_show.add_argument("--masked", action="store_true", default=True)
    config_secret = config_commands.add_parser("set-secret")
    config_secret.add_argument("name")

    commands.add_parser("doctor", help="Check installation, configuration and ledger")
    gui = commands.add_parser("gui", help="Open the local finance interface in the browser")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")

    llm = commands.add_parser("llm", help="Show and check optional model configuration")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    llm_commands.add_parser("show")
    llm_commands.add_parser("test")

    auth = commands.add_parser("auth", help="Authenticate locally with the experimental bank API")
    auth.add_argument("provider", choices=("comdirect",))
    auth.add_argument("connection")

    comdirect = commands.add_parser(
        "comdirect", help="Diagnose and recover experimental bank connections"
    )
    comdirect_commands = comdirect.add_subparsers(
        dest="comdirect_command",
        required=True,
    )
    for action in ("diagnose", "logout"):
        command = comdirect_commands.add_parser(action)
        command.add_argument("connection")
    comdirect_recover = comdirect_commands.add_parser("recover")
    comdirect_recover.add_argument("connection")
    comdirect_recover.add_argument("--resume")
    comdirect_recover.add_argument("--accounts", nargs="+")
    comdirect_recover.add_argument("--from", dest="booking_from")
    comdirect_recover.add_argument("--to", dest="booking_to")

    accounts = commands.add_parser("accounts", help="Manage accounts and their settings")
    account_commands = accounts.add_subparsers(
        dest="accounts_command",
        required=True,
    )
    account_list = account_commands.add_parser("list")
    account_list.add_argument("--enabled-only", action="store_true")
    account_show = account_commands.add_parser("show")
    account_show.add_argument("account_id")
    account_register = account_commands.add_parser("register")
    account_register.add_argument("provider_kind")
    account_register.add_argument("provider_name")
    account_register.add_argument("external_id")
    account_register.add_argument("alias")
    account_register.add_argument("--currency", default="EUR")
    account_register.add_argument(
        "--type",
        dest="account_type",
        default="checking",
        choices=(
            "checking",
            "credit_card",
            "savings",
            "cash",
            "loan",
            "asset",
            "liability",
        ),
    )
    account_manual = account_commands.add_parser("create-manual")
    account_manual.add_argument("name")
    account_manual.add_argument(
        "--type",
        dest="account_type",
        required=True,
        choices=(
            "checking",
            "credit_card",
            "savings",
            "cash",
            "loan",
            "asset",
            "liability",
        ),
    )
    account_manual.add_argument("--currency", default="EUR")
    account_manual.add_argument("--opening-balance")
    account_manual.add_argument("--balance-date")
    account_manual.add_argument("--idempotency-key", required=True)
    account_alias = account_commands.add_parser("alias")
    account_alias.add_argument("account_id")
    account_alias.add_argument("name")
    account_alias.add_argument("--version", type=int)
    for action in ("enable", "disable"):
        account_state = account_commands.add_parser(action)
        account_state.add_argument("account_id")
        account_state.add_argument("--version", type=int)
    account_group = account_commands.add_parser("group-create")
    account_group.add_argument("name")
    account_group.add_argument("account_ids", nargs="+")
    account_group.add_argument("--description", default="")
    account_group_update = account_commands.add_parser("group-update")
    account_group_update.add_argument("group")
    account_group_update.add_argument("account_ids", nargs="+")
    account_group_update.add_argument("--version", type=int, required=True)
    account_commands.add_parser("groups")
    account_options = account_commands.add_parser("options")
    account_options.add_argument("account_id")
    account_options_set = account_commands.add_parser("options-set")
    account_options_set.add_argument("account_id")
    account_options_set.add_argument(
        "--kind", required=True, choices=("category", "tag", "dimension_value")
    )
    account_options_set.add_argument("--code", action="append", default=[])
    account_columns = account_commands.add_parser("columns")
    account_columns.add_argument("account_id")
    account_columns_set = account_commands.add_parser("columns-set")
    account_columns_set.add_argument("account_id")
    account_columns_set.add_argument("--code", action="append", default=[])
    account_profiles = account_commands.add_parser("profiles", help="Create profiles")
    account_profiles.add_argument("account_id")
    account_profiles_set = account_commands.add_parser("profiles-set")
    account_profiles_set.add_argument("account_id")
    account_profiles_set.add_argument("--profile", action="append", default=[])
    account_stages = account_commands.add_parser("stages")
    account_stages.add_argument("account_id")
    account_stage_set = account_commands.add_parser("stage-set")
    account_stage_set.add_argument("account_id")
    account_stage_set.add_argument(
        "--stage", choices=("rule", "history", "hint", "llm"), required=True
    )
    account_stage_set.add_argument("--enabled", action="store_true")
    account_stage_set.add_argument("--disabled", action="store_true")

    source_reconciliation = commands.add_parser(
        "source-reconciliation", help="Reconcile source accounts through previews"
    )
    source_reconciliation_commands = source_reconciliation.add_subparsers(
        dest="source_reconciliation_command", required=True
    )
    for action in ("preview", "run"):
        command = source_reconciliation_commands.add_parser(action)
        command.add_argument("--source-account", required=True)
        command.add_argument("--target-account", required=True)
        if action == "run":
            command.add_argument("--fingerprint", required=True)

    import_workflow = commands.add_parser(
        "import", help="Preview and run file imports (recommended)"
    )
    import_commands = import_workflow.add_subparsers(
        dest="import_command",
        required=True,
    )
    import_preview = import_commands.add_parser("preview")
    import_preview.add_argument(
        "--source",
        choices=available_sources(),
        required=True,
    )
    import_preview.add_argument("--account")
    import_preview.add_argument("--profile")
    import_preview.add_argument("path")
    import_run = import_commands.add_parser("run")
    import_run.add_argument("preview_id")
    sync = commands.add_parser(
        "sync", help="Synchronize read-only account data (experimental Comdirect API)"
    )
    sync.add_argument("provider", choices=("comdirect",))
    sync.add_argument("connection")
    sync.add_argument("--accounts", nargs="+")
    sync.add_argument("--from", dest="booking_from")
    sync.add_argument("--to", dest="booking_to")
    sync.add_argument("--resume")

    list_cmd = commands.add_parser("transactions", help="List transactions in account scope")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.add_argument("--accounts", nargs="+")
    list_cmd.add_argument("--group")
    list_cmd.add_argument("--from", dest="booking_from")
    list_cmd.add_argument("--to", dest="booking_to")
    list_cmd.add_argument(
        "--status",
        nargs="+",
        choices=("pending", "booked", "reversed", "corrected"),
    )
    list_cmd.add_argument("--all-statuses", action="store_true")

    manual = commands.add_parser("manual", help="Create and edit manual transactions")
    manual_commands = manual.add_subparsers(dest="manual_command", required=True)
    manual_add = manual_commands.add_parser("add")
    manual_add.add_argument("account_id")
    manual_add.add_argument("amount")
    manual_add.add_argument("booking_date")
    manual_add.add_argument("purpose")
    manual_add.add_argument("--valuta-date")
    manual_add.add_argument("--counterparty")
    manual_add.add_argument("--reference")
    manual_add.add_argument(
        "--status",
        default="booked",
        choices=("pending", "booked", "reversed", "corrected"),
    )
    manual_add.add_argument("--idempotency-key", required=True)
    manual_add.add_argument("--profile")
    manual_add.add_argument("--category")
    manual_add.add_argument("--tag", action="append", default=[])
    manual_add.add_argument("--dimension", action="append", default=[])
    manual_update = manual_commands.add_parser("update")
    manual_update.add_argument("transaction_id")
    manual_update.add_argument("--version", type=int, required=True)
    manual_update.add_argument("--date", dest="booking_date")
    manual_update.add_argument("--amount")
    manual_update.add_argument("--purpose")
    manual_update.add_argument("--counterparty")
    manual_update.add_argument("--reference")
    manual_update.add_argument("--valuta-date")

    search = commands.add_parser("search", help="Search transactions with combined criteria")
    search.add_argument("--text")
    search.add_argument("--accounts", nargs="+")
    search.add_argument("--group")
    search.add_argument("--from", dest="booking_from")
    search.add_argument("--to", dest="booking_to")
    search.add_argument("--amount-min")
    search.add_argument("--amount-max")
    search.add_argument("--currency", action="append", default=[])
    search.add_argument(
        "--status",
        action="append",
        default=[],
        choices=("pending", "booked", "reversed", "corrected"),
    )
    search.add_argument("--category", action="append", default=[])
    search.add_argument("--classification-source", action="append", default=[])
    search.add_argument("--tag", action="append", default=[])
    search.add_argument(
        "--dimension",
        action="append",
        default=[],
        help="Dimension=Value1,Value2",
    )
    search.add_argument("--merchant")
    search.add_argument("--document-status", choices=("matched", "unmatched"))
    search.add_argument("--unreviewed", action="store_true")
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--offset", type=int, default=0)

    tags = commands.add_parser("tags", help="Manage tags")
    tag_commands = tags.add_subparsers(dest="tags_command", required=True)
    tag_create = tag_commands.add_parser("create")
    tag_create.add_argument("name")
    tag_commands.add_parser("list")
    for action in ("add", "remove"):
        tag_action = tag_commands.add_parser(action)
        tag_action.add_argument("transaction_id")
        tag_action.add_argument("tag")
        tag_action.add_argument("--version", type=int, required=True)

    dimensions = commands.add_parser("dimensions", help="Manage dimensions and assignments")
    dimension_commands = dimensions.add_subparsers(dest="dimensions_command", required=True)
    dimension_create = dimension_commands.add_parser("create")
    dimension_create.add_argument("code")
    dimension_create.add_argument("name")
    dimension_value = dimension_commands.add_parser("value-add")
    dimension_value.add_argument("dimension")
    dimension_value.add_argument("code")
    dimension_value.add_argument("name")
    dimension_rename = dimension_commands.add_parser("rename")
    dimension_rename.add_argument("code")
    dimension_rename.add_argument("name")
    dimension_rename.add_argument("--version", type=int, required=True)
    dimension_deactivate = dimension_commands.add_parser("deactivate")
    dimension_deactivate.add_argument("code")
    dimension_deactivate.add_argument("--version", type=int, required=True)
    dimension_delete = dimension_commands.add_parser("delete")
    dimension_delete.add_argument("code")
    dimension_value_rename = dimension_commands.add_parser("value-rename")
    dimension_value_rename.add_argument("dimension")
    dimension_value_rename.add_argument("code")
    dimension_value_rename.add_argument("name")
    dimension_value_rename.add_argument("--version", type=int, required=True)
    dimension_value_deactivate = dimension_commands.add_parser("value-deactivate")
    dimension_value_deactivate.add_argument("dimension")
    dimension_value_deactivate.add_argument("code")
    dimension_value_deactivate.add_argument("--version", type=int, required=True)
    dimension_value_delete = dimension_commands.add_parser("value-delete")
    dimension_value_delete.add_argument("dimension")
    dimension_value_delete.add_argument("code")
    dimension_commands.add_parser("list")
    dimension_set = dimension_commands.add_parser("set")
    dimension_set.add_argument("transaction_id")
    dimension_set.add_argument("dimension")
    dimension_set.add_argument("value")
    dimension_set.add_argument("--version", type=int, required=True)
    dimension_clear = dimension_commands.add_parser("clear")
    dimension_clear.add_argument("transaction_id")
    dimension_clear.add_argument("dimension")
    dimension_clear.add_argument("--version", type=int, required=True)

    merchants = commands.add_parser("merchants", help="Manage merchants and assignments")
    merchant_commands = merchants.add_subparsers(dest="merchants_command", required=True)
    merchant_create = merchant_commands.add_parser("create")
    merchant_create.add_argument("name")
    merchant_alias = merchant_commands.add_parser("alias-add")
    merchant_alias.add_argument("merchant")
    merchant_alias.add_argument("alias")
    merchant_commands.add_parser("list")
    merchant_set = merchant_commands.add_parser("set")
    merchant_set.add_argument("transaction_id")
    merchant_set.add_argument("merchant")
    merchant_set.add_argument("--version", type=int, required=True)

    review = commands.add_parser("review", help="Review open items")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--status", default="open", choices=("open", "resolved", "dismissed"))
    review_list.add_argument("--type", dest="item_type")
    review_list.add_argument("--accounts", nargs="+")
    review_list.add_argument("--group")
    review_list.add_argument("--limit", type=int, default=100)
    for action in ("resolve", "dismiss"):
        review_action = review_commands.add_parser(action)
        review_action.add_argument("review_id")
        review_action.add_argument("--version", type=int, required=True)

    bulk = commands.add_parser("bulk", help="Preview and run bulk changes")
    bulk_commands = bulk.add_subparsers(dest="bulk_command", required=True)
    bulk_preview = bulk_commands.add_parser("preview")
    bulk_preview.add_argument(
        "action",
        choices=(
            "categorize",
            "tag_add",
            "tag_remove",
            "dimension_set",
            "dimension_clear",
            "merchant_set",
            "edit_fields",
        ),
    )
    bulk_preview.add_argument("transaction_ids", nargs="+")
    bulk_preview.add_argument("--profile")
    bulk_preview.add_argument("--category")
    bulk_preview.add_argument("--tag")
    bulk_preview.add_argument("--dimension")
    bulk_preview.add_argument("--value")
    bulk_preview.add_argument("--merchant")
    bulk_preview.add_argument(
        "--patch-json",
        help="JSON object for action=edit_fields",
    )
    bulk_run = bulk_commands.add_parser("run")
    bulk_run.add_argument("preview_id")
    bulk_run.add_argument("--fingerprint")

    rates = commands.add_parser("rates", help="Manage local exchange rates")
    rate_commands = rates.add_subparsers(dest="rates_command", required=True)
    rate_add = rate_commands.add_parser("add")
    rate_add.add_argument("source_currency")
    rate_add.add_argument("target_currency")
    rate_add.add_argument("rate")
    rate_add.add_argument("--date", required=True)
    rate_add.add_argument("--source", default="manual")
    rate_add.add_argument("--source-version", default="manual-v1")
    rate_add.add_argument("--idempotency-key", required=True)
    rate_list = rate_commands.add_parser("list")
    rate_list.add_argument("--source-currency")
    rate_list.add_argument("--target-currency")
    rate_list.add_argument("--limit", type=int, default=100)

    analyses = commands.add_parser("analyses", help="Define and run reproducible analyses")
    analysis_commands = analyses.add_subparsers(dest="analyses_command", required=True)
    analysis_create = analysis_commands.add_parser("create")
    analysis_create.add_argument("code")
    analysis_create.add_argument("name")
    analysis_create.add_argument("--description", default="")
    _add_analysis_definition_arguments(analysis_create)
    analysis_update = analysis_commands.add_parser("update")
    analysis_update.add_argument("analysis")
    analysis_update.add_argument("--definition", required=True)
    analysis_update.add_argument("--name")
    analysis_update.add_argument("--description")
    analysis_update.add_argument("--version", type=int, required=True)
    analysis_commands.add_parser("list")
    analysis_show = analysis_commands.add_parser("show")
    analysis_show.add_argument("analysis")
    analysis_run = analysis_commands.add_parser("run")
    analysis_run.add_argument("analysis")
    analysis_run.add_argument("--as-of")
    analysis_run.add_argument("--formats", nargs="+", choices=("json", "xlsx", "html"))
    analysis_run.add_argument("--output")

    transaction_status = commands.add_parser(
        "transaction-status", help="Change booking status with version protection"
    )
    transaction_status.add_argument("transaction_id")
    transaction_status.add_argument(
        "status", choices=("pending", "booked", "reversed", "corrected")
    )
    transaction_status.add_argument("--version", type=int, required=True)
    transaction_status.add_argument("--reason", required=True)

    transaction_history = commands.add_parser(
        "transaction-history", help="Show transaction change history"
    )
    transaction_history.add_argument("transaction_id")

    transaction_note = commands.add_parser(
        "transaction-note", help="Change transaction note with version protection"
    )
    transaction_note.add_argument("transaction_id")
    transaction_note.add_argument("--text", required=True)
    transaction_note.add_argument("--version", type=int, required=True)

    relations = commands.add_parser(
        "relations", help="Review transfers and other transaction relations"
    )
    relation_commands = relations.add_subparsers(dest="relations_command", required=True)
    relation_suggest = relation_commands.add_parser("suggest")
    relation_suggest.add_argument(
        "relation_type",
        choices=("transfer", "refund", "reversal", "correction"),
    )
    relation_suggest.add_argument("--transaction")
    relation_suggest.add_argument("--accounts", nargs="+")
    relation_suggest.add_argument("--group")
    relation_suggest.add_argument("--limit", type=int, default=100)
    relation_list = relation_commands.add_parser("list")
    relation_list.add_argument(
        "--type",
        dest="relation_type",
        choices=("transfer", "refund", "reversal", "correction"),
    )
    relation_list.add_argument(
        "--status",
        choices=("suggested", "confirmed", "rejected", "resolved"),
    )
    relation_list.add_argument("--transaction")
    relation_list.add_argument("--limit", type=int, default=100)
    relation_create = relation_commands.add_parser("create")
    relation_create.add_argument(
        "relation_type",
        choices=("transfer", "refund", "reversal", "correction"),
    )
    relation_create.add_argument("source_transaction_id")
    relation_create.add_argument("target_transaction_id")
    relation_create.add_argument("--source-version", type=int, required=True)
    relation_create.add_argument("--target-version", type=int, required=True)
    relation_create.add_argument("--fee", default="0")
    relation_create.add_argument("--reason", action="append", default=[])
    for action in ("confirm", "reject", "resolve"):
        relation_action = relation_commands.add_parser(action)
        relation_action.add_argument("relation_id")
        relation_action.add_argument("--version", type=int, required=True)

    balances = commands.add_parser("balances", help="Record and reconcile balances")
    balance_commands = balances.add_subparsers(dest="balances_command", required=True)
    balance_add = balance_commands.add_parser("add")
    balance_add.add_argument("account_id")
    balance_add.add_argument("amount")
    balance_add.add_argument("--date", required=True)
    balance_add.add_argument("--source", default="manual")
    balance_add.add_argument("--external-id")
    balance_add.add_argument("--version", type=int, required=True)
    balance_list = balance_commands.add_parser("list")
    balance_list.add_argument("--account")
    balance_list.add_argument("--limit", type=int, default=100)
    balance_reconcile = balance_commands.add_parser("reconcile")
    balance_reconcile.add_argument("snapshot_id")
    reconciliation_list = balance_commands.add_parser("reconciliations")
    reconciliation_list.add_argument("--account")
    reconciliation_list.add_argument("--limit", type=int, default=100)

    classification = commands.add_parser(
        "classification", help="Explain classification and test rules"
    )
    classification_commands = classification.add_subparsers(
        dest="classification_diagnostic_command", required=True
    )
    classification_explain = classification_commands.add_parser("explain")
    classification_explain.add_argument("transaction_id")
    classification_explain.add_argument("--profile", required=True)
    classification_test_rule = classification_commands.add_parser("test-rule")
    classification_test_rule.add_argument("--pattern", required=True)
    classification_test_rule.add_argument("--profile", required=True)
    classification_test_rule.add_argument("--account")
    classification_test_rule.add_argument("--limit", type=int, default=1000)

    classifications = commands.add_parser(
        "classifications", help="Classify transactions and review suggestions"
    )
    classification_commands = classifications.add_subparsers(
        dest="classification_command", required=True
    )
    classify = classification_commands.add_parser("set")
    classify.add_argument("transaction_id")
    classify.add_argument("profile")
    classify.add_argument("category")
    classify.add_argument("--version", type=int)
    classify.add_argument("--suggest", action="store_true")

    rules = classification_commands.add_parser("apply-rules")
    rules.add_argument("transaction_id")
    rules.add_argument("profile")
    automatic = classification_commands.add_parser("deterministic")
    automatic.add_argument("transaction_id")
    automatic.add_argument("profile")

    classification_run = classification_commands.add_parser("run")
    classification_run.add_argument("profile")
    classification_run.add_argument("--transaction")
    classification_run.add_argument("--limit", type=int, default=100)
    classification_run.add_argument("--accounts", nargs="+")
    classification_run.add_argument("--group")
    classification_run.add_argument("--preview")
    classification_run.add_argument("--fingerprint")
    classification_preview = classification_commands.add_parser("preview")
    classification_preview.add_argument("profile")
    classification_preview.add_argument("--transaction", action="append")
    classification_preview.add_argument("--limit", type=int, default=100)
    classification_preview.add_argument("--accounts", nargs="+")
    classification_preview.add_argument("--group")
    classification_review = classification_commands.add_parser("review")
    classification_review.add_argument("--profile")
    classification_review.add_argument("--status", default="suggested")
    classification_review.add_argument("--limit", type=int, default=100)
    for action in ("accept", "reject"):
        classification_action = classification_commands.add_parser(action)
        classification_action.add_argument("classification_id")
        classification_action.add_argument("--version", type=int, required=True)

    profiles = commands.add_parser("profiles", help="Create profiles")
    profile_commands = profiles.add_subparsers(dest="profiles_command", required=True)
    profile = profile_commands.add_parser("create")
    profile.add_argument("code")
    profile.add_argument("name")
    profile.add_argument("--description", default="")

    wiso_trees = commands.add_parser(
        "wiso-trees", help="Import WISO categories and configured dimensions"
    )
    wiso_tree_commands = wiso_trees.add_subparsers(dest="wiso_tree_command", required=True)
    for action in ("preview", "run"):
        wiso_tree = wiso_tree_commands.add_parser(action)
        wiso_tree.add_argument("--categories", type=Path, required=True)
        wiso_tree.add_argument("--tax", type=Path, required=True)
        wiso_tree.add_argument("--free-1", type=Path)
        wiso_tree.add_argument("--free-2", type=Path)
        wiso_tree.add_argument("--free-3", type=Path)
        if action == "run":
            wiso_tree.add_argument("--fingerprint", required=True)
    rules_config = commands.add_parser("rules", help="Manage classification rules")
    rules_commands = rules_config.add_subparsers(dest="rules_command", required=True)
    rule = rules_commands.add_parser("add")
    rule.add_argument("profile")
    rule.add_argument("name")
    rule.add_argument("pattern")
    rule.add_argument("category")
    rule.add_argument("--priority", type=int, default=100)
    rule.add_argument("--account")
    rules_list = rules_commands.add_parser("list")
    rules_list.add_argument("--profile")
    rules_list.add_argument("--account")
    rules_update = rules_commands.add_parser("update")
    rules_update.add_argument("rule_id")
    rules_update.add_argument("--version", type=int, required=True)
    rules_update.add_argument("--name")
    rules_update.add_argument("--pattern")
    rules_update.add_argument("--category")
    rules_update.add_argument("--priority", type=int)
    rules_update.add_argument("--active", action=argparse.BooleanOptionalAction, default=None)
    rules_update.add_argument("--account")
    rules_delete = rules_commands.add_parser("delete")
    rules_delete.add_argument("rule_id")
    rules_delete.add_argument("--version", type=int, required=True)
    hints_config = commands.add_parser("hints", help="Manage classification hints")
    hints_commands = hints_config.add_subparsers(dest="hints_command", required=True)
    hint = hints_commands.add_parser("add")
    hint.add_argument("profile")
    hint.add_argument("text")
    hint.add_argument("--pattern")
    hint.add_argument("--category")
    hint.add_argument("--priority", type=int, default=100)
    hint.add_argument("--account")
    hints_list = hints_commands.add_parser("list")
    hints_list.add_argument("--profile")
    hints_list.add_argument("--account")
    hints_update = hints_commands.add_parser("update")
    hints_update.add_argument("hint_id")
    hints_update.add_argument("--version", type=int, required=True)
    hints_update.add_argument("--text")
    hints_update.add_argument("--pattern")
    hints_update.add_argument("--category")
    hints_update.add_argument("--priority", type=int)
    hints_update.add_argument("--active", action=argparse.BooleanOptionalAction, default=None)
    hints_update.add_argument("--account")
    hints_delete = hints_commands.add_parser("delete")
    hints_delete.add_argument("hint_id")
    hints_delete.add_argument("--version", type=int, required=True)

    report = commands.add_parser(
        "report", help="Generate generic or explicitly extended local reports"
    )
    report.add_argument("--profile")
    report.add_argument(
        "--extension", help="Name of an explicitly configured local report extension"
    )
    report.add_argument("--output")
    report.add_argument("--accounts", nargs="+")
    report.add_argument("--group")
    report.add_argument("--from", dest="booking_from")
    report.add_argument("--to", dest="booking_to")
    report.add_argument("--formats", nargs="+", choices=("json", "xlsx", "html"))
    report.add_argument("--currency")
    deliveries = commands.add_parser(
        "deliveries", help="Preview report delivery and confirm locally"
    )
    delivery_commands = deliveries.add_subparsers(dest="delivery_command", required=True)
    delivery_list = delivery_commands.add_parser("list")
    delivery_list.add_argument("--limit", type=int, default=20)
    delivery_preview = delivery_commands.add_parser("preview")
    delivery_preview.add_argument("target")
    delivery_preview.add_argument("--report-run", default="latest")
    delivery_send = delivery_commands.add_parser("send")
    delivery_send.add_argument("preview_id")
    delivery_send.add_argument("--fingerprint", required=True)
    delivery_send.add_argument("--idempotency-key", required=True)
    delivery_send.add_argument("--yes", action="store_true", required=True)
    documents = commands.add_parser("documents", help="Register, import and match documents")
    document_commands = documents.add_subparsers(dest="documents_command", required=True)
    document = document_commands.add_parser("register")
    document.add_argument("filename")
    document.add_argument("--amount")
    document.add_argument("--merchant")
    document_import = document_commands.add_parser("import")
    document_import.add_argument("path")
    document_import.add_argument("--amount")
    document_import.add_argument("--date")
    document_import.add_argument("--merchant")
    document_import.add_argument("--reference")
    document_import.add_argument("--account")
    document_import.add_argument("--profile")
    document_preview = document_commands.add_parser("preview")
    document_preview.add_argument("path")
    document_run = document_commands.add_parser("run")
    document_run.add_argument("preview_id")
    document_run.add_argument("--fingerprint", required=True)
    document_list = document_commands.add_parser("list")
    document_list.add_argument("--status")
    document_candidates = document_commands.add_parser("candidates")
    document_candidates.add_argument("document_id")
    document_candidates.add_argument("--accounts", nargs="+")
    document_candidates.add_argument("--group")
    document_candidates.add_argument("--profile")
    document_preview_matches = document_commands.add_parser("preview-matches")
    document_preview_matches.add_argument("document_id")
    document_preview_matches.add_argument("--accounts", nargs="+")
    document_preview_matches.add_argument("--group")
    document_preview_matches.add_argument("--profile")
    document_preview_matches.add_argument("--limit", type=int, default=100)
    document_apply_matches = document_commands.add_parser("apply-matches")
    document_apply_matches.add_argument("document_id")
    document_apply_matches.add_argument("--transaction", action="append", required=True)
    document_apply_matches.add_argument("--fingerprint", required=True)
    document_apply_matches.add_argument("--document-version", type=int, required=True)
    document_apply_matches.add_argument("--append", action="store_true", default=True)
    for action in ("accept", "reject", "unmatch"):
        document_action = document_commands.add_parser(action)
        document_action.add_argument("match_id")
        document_action.add_argument("--version", type=int, required=True)
    document_assign = document_commands.add_parser("assign")
    document_assign.add_argument("document_id")
    document_assign.add_argument("transaction_id")
    document_assign.add_argument("--append", action="store_true")
    document_assign.add_argument("--document-version", type=int)
    document_assign.add_argument("--transaction-version", type=int)

    splits = commands.add_parser("splits", help="Manage splits with version protection")
    split_commands = splits.add_subparsers(dest="splits_command", required=True)
    for action in ("create", "update"):
        split_create = split_commands.add_parser(action)
        split_create.add_argument("transaction_id")
        split_create.add_argument("profile")
        split_create.add_argument(
            "--part", action="append", required=True, help="Amount:Category[:Note]"
        )
        split_create.add_argument("--version", type=int, required=True)
    split_revert = split_commands.add_parser("revert")
    split_revert.add_argument("transaction_id")
    split_revert.add_argument("--version", type=int, required=True)

    database = commands.add_parser("database", help="Manage database operations")
    database_commands = database.add_subparsers(
        dest="database_command",
        required=True,
    )
    database_backup = database_commands.add_parser("backup")
    database_backup.add_argument("--output")
    database_restore = database_commands.add_parser("restore")
    database_restore.add_argument("backup")
    database_restore.add_argument(
        "--yes",
        action="store_true",
        help="Confirm replacement of the current database.",
    )
    database_export = database_commands.add_parser("export")
    database_export.add_argument("--output", required=True)

    portability = commands.add_parser("portability", help="Export, back up and safely delete data")
    portability_commands = portability.add_subparsers(
        dest="portability_command",
        required=True,
    )
    portability_export = portability_commands.add_parser("export")
    portability_export.add_argument("--output", required=True)
    portability_backup = portability_commands.add_parser("backup")
    portability_backup.add_argument("--output", required=True)
    portability_backup.add_argument(
        "--password-secret",
        default="ACCOUNT_EVAL_BACKUP_PASSWORD",
    )
    portability_verify = portability_commands.add_parser("verify")
    portability_verify.add_argument("path")
    portability_verify.add_argument("--password-secret")
    portability_restore = portability_commands.add_parser("restore")
    portability_restore.add_argument("path")
    portability_restore.add_argument("--password-secret")
    portability_restore.add_argument("--yes", action="store_true")
    portability_commands.add_parser("integrity")
    deletion_preview = portability_commands.add_parser("delete-preview")
    deletion_preview.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=("database", "documents", "reports", "backups", "exports"),
    )
    deletion_run = portability_commands.add_parser("delete-run")
    deletion_run.add_argument("preview_id")
    deletion_run.add_argument("--fingerprint", required=True)
    deletion_run.add_argument("--yes", action="store_true")

    audit = commands.add_parser("audit", help="Show audit events")
    audit.add_argument("--entity-type")
    audit.add_argument("--limit", type=int, default=100)
    return parser


def _add_analysis_definition_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", nargs="+")
    parser.add_argument("--group")
    parser.add_argument("--profile")
    parser.add_argument("--from", dest="booking_from")
    parser.add_argument("--to", dest="booking_to")
    parser.add_argument("--relative-months", type=int)
    parser.add_argument("--text")
    parser.add_argument("--amount-min")
    parser.add_argument("--amount-max")
    parser.add_argument("--currency", action="append", default=[])
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        choices=("pending", "booked", "reversed", "corrected"),
    )
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--dimension", action="append", default=[])
    parser.add_argument("--merchant")
    parser.add_argument("--document-status", choices=("matched", "unmatched"))
    parser.add_argument("--base-currency")
    parser.add_argument("--fixed-cost-tag", default="fixed")
    parser.add_argument("--stale-balance-days", type=int, default=31)
    parser.add_argument(
        "--sections",
        nargs="+",
        choices=("cashflow", "income_expense", "fixed_costs", "trends", "net_worth"),
    )
    parser.add_argument("--formats", nargs="+", choices=("json", "xlsx", "html"))
    parser.add_argument("--group-by", action="append", default=[])


def _print_json(value: Any) -> None:
    rendered = success(value) if _OUTPUT_MODE == "json" else value
    if _OUTPUT_MODE == "human":
        if isinstance(value, list):
            for item in value:
                print(_human_line(item))
        else:
            print(_human_line(value))
        return
    print(json.dumps(rendered, ensure_ascii=False, indent=2, default=str))


_OUTPUT_MODE = "legacy-json"


def _human_line(value: Any) -> str:
    if isinstance(value, dict):
        return " | ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _loaded(args: argparse.Namespace) -> LoadedConfig:
    if current := CURRENT_WORKSPACE.get():
        return current
    overrides = {"database": {"path": args.db}} if args.db else None
    return load_config(args.config, overrides=overrides)


def _handle_project_commands(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int | None:
    if args.command == "init":
        try:
            config_path = initialize_project_config(
                args.config,
                force=args.force,
            )
        except FileExistsError as error:
            parser.error(str(error))
        loaded = load_config(
            config_path,
            overrides={"database": {"path": args.db}} if args.db else None,
        )
        initialized_repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
        install_builtin_profiles(initialized_repository)
        _print_json(
            {
                "configuration": config_path,
                "database": loaded.config.database.path,
            }
        )
        return 0

    if args.command == "setup":
        answers = SetupAnswers(
            comdirect_enabled=args.comdirect,
            llm_preset=args.llm_preset,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            base_currency=args.base_currency,
        ).validated()
        summary = {
            "comdirect_enabled": answers.comdirect_enabled,
            "llm_preset": answers.llm_preset,
            "llm_model": answers.llm_model,
            "llm_base_url": answers.llm_base_url or LLM_PRESETS[answers.llm_preset][0],
            "base_currency": answers.base_currency,
            "secrets": "not written",
        }
        if not args.yes:
            _print_json({"preview": summary})
            if input("Write local configuration? [y/N] ").strip().lower() not in {"y", "yes"}:
                _print_json({"status": "cancelled"})
                return 1
        config_path = Path(args.config or "account-eval.toml")
        configured = configure_local_project(config_path, answers)
        configured["agent_runtime"] = configure_agent_launcher(config_path.parent or ".")
        _print_json(configured)
        return 0

    if args.command == "demo":
        if args.demo_command == "create":
            _print_json(create_demo_workspace(args.path, force=args.force))
            return 0
        remove_demo_workspace(args.path)
        _print_json({"status": "deleted", "path": str(Path(args.path).resolve())})
        return 0

    if args.command == "agents" and args.agents_command == "configure":
        current = CURRENT_WORKSPACE.get()
        root = args.agent_workspace or (current.project_path.parent if current else ".")
        _print_json(configure_agent_launcher(root))
        return 0

    if args.command == "agents" and args.agents_command == "doctor":
        current = CURRENT_WORKSPACE.get()
        root = args.agent_workspace or (current.project_path.parent if current else ".")
        _print_json(
            diagnose_agent_runtime(
                root,
                config_path=args.config or "account-eval.toml",
            )
        )
        return 0

    if args.command == "self-test":
        _print_json(run_offline_self_test())
        return 0

    if args.command == "contracts":
        _print_json(versions())
        return 0

    if args.command == "completion":
        print(_completion_script(args.shell))
        return 0

    loaded = _loaded(args)
    if args.command == "config":
        if args.config_command == "validate":
            _print_json(
                {
                    "status": "ok",
                    "configuration": loaded.project_path,
                    "local_override": (loaded.local_path if loaded.local_path.exists() else None),
                }
            )
            return 0
        if args.config_command == "show":
            _print_json(masked_config(loaded.config))
            return 0
        if args.config_command == "set-secret":
            secret_name = args.name.casefold()
            if secret_name in {
                "comdirect.password",
                "comdirect.pin",
                "comdirect_password",
                "comdirect_pin",
            } or (
                secret_name.startswith("comdirect.")
                and secret_name.endswith((".password", ".pin", ".timed-session"))
            ):
                parser.error(
                    "Comdirect PINs and timed sessions are managed locally and cannot be stored"
                )
            value = getpass.getpass(f"Secret {args.name}: ")
            SecretStore(dotenv_path=loaded.project_path.with_name(".env")).set(args.name, value)
            _print_json({"stored": args.name, "backend": "keyring"})
            return 0
    if args.command == "doctor":
        diagnostics = run_diagnostics(loaded)
        _print_json([item.as_dict() for item in diagnostics])
        return 1 if any(item.status == "error" for item in diagnostics) else 0
    if args.command == "llm":
        if args.llm_command == "show":
            _print_json(
                {
                    "enabled": loaded.config.llm.enabled,
                    "mode": loaded.config.llm.mode,
                    "provider": loaded.config.llm.provider,
                    "base_url": loaded.config.llm.base_url,
                    "model": loaded.config.llm.model,
                    "allow_cloud": loaded.config.llm.allow_cloud,
                    "api_key_secret": loaded.config.llm.api_key_secret,
                }
            )
            return 0
        llm_provider = create_llm_provider(
            loaded.config,
            SecretStore(dotenv_path=loaded.project_path.with_name(".env")),
        )
        if llm_provider is None:
            parser.error("LLM ist in account-eval.toml deaktiviert")
        _print_json(llm_provider.test_connection())
        return 0
    if args.command == "auth" and args.provider == "comdirect":
        comdirect_connection = _comdirect_connection(loaded, args.connection)
        if not comdirect_connection.enabled:
            parser.error(f"Comdirect connection '{args.connection}' is not enabled")
        if comdirect_connection.session_mode != "keychain_timed":
            parser.error(
                "auth comdirect <connection> is only available with session_mode = "
                "keychain_timed; use sync comdirect <connection> for the secure default"
            )
        state = _authenticate_comdirect(args.connection, comdirect_connection)
        _store_timed_comdirect_session(args.connection, comdirect_connection, state)
        _print_json(
            {
                "provider": "comdirect",
                "connection": args.connection,
                "authenticated": True,
                "session_backend": "keyring",
                "valid_until": _timed_session_expiry(
                    state,
                    comdirect_connection.keychain_session_max_age_hours,
                ).isoformat(),
            }
        )
        return 0
    if args.command == "database":
        database = LedgerDatabase(loaded.config.database.path)
        if args.database_command == "backup":
            print(database.backup(args.output))
            return 0
        if args.database_command == "restore":
            if not args.yes:
                parser.error("database restore replaces the current database; repeat with --yes")
            database.restore(args.backup)
            print(database.path)
            return 0
        if args.database_command == "export":
            print(database.export_json(args.output))
            return 0
    return None


def _handle_account_commands(
    args: argparse.Namespace,
    repo: LedgerRepository,
) -> int | None:
    if args.command != "accounts":
        return None
    if args.accounts_command == "list":
        _print_json(
            [
                safe_financial_record(account)
                for account in repo.list_accounts(enabled_only=args.enabled_only)
            ]
        )
        return 0
    if args.accounts_command == "show":
        _print_json(safe_financial_record(repo.get_account(args.account_id)))
        return 0
    if args.accounts_command == "register":
        provider_id = repo.ensure_provider(
            args.provider_kind,
            args.provider_name,
        )
        account_id, created = repo.ensure_account(
            provider_id,
            args.external_id,
            args.alias,
            args.currency,
            account_kind=args.account_type,
        )
        _print_json(
            {
                "account_id": account_id,
                "created": created,
                "account_type": args.account_type,
            }
        )
        return 0
    if args.accounts_command == "create-manual":
        _print_json(
            OrganizationService(repo).create_manual_account(
                args.name,
                account_kind=args.account_type,
                currency=args.currency,
                idempotency_key=args.idempotency_key,
                opening_balance=(
                    Decimal(args.opening_balance) if args.opening_balance is not None else None
                ),
                balance_date=(date.fromisoformat(args.balance_date) if args.balance_date else None),
            )
        )
        return 0
    if args.accounts_command == "alias":
        version = repo.update_account_alias(
            args.account_id,
            args.name,
            expected_version=args.version,
        )
        _print_json({"account_id": args.account_id, "version": version})
        return 0
    if args.accounts_command in ("enable", "disable"):
        enabled = args.accounts_command == "enable"
        version = repo.set_account_enabled(
            args.account_id,
            enabled,
            expected_version=args.version,
        )
        _print_json(
            {
                "account_id": args.account_id,
                "enabled": enabled,
                "version": version,
            }
        )
        return 0
    if args.accounts_command == "group-create":
        identifier = repo.create_account_group(
            args.name,
            args.account_ids,
            args.description,
        )
        _print_json({"account_group_id": identifier})
        return 0
    if args.accounts_command == "group-update":
        version = repo.update_account_group_members(
            args.group,
            args.account_ids,
            expected_version=args.version,
        )
        _print_json({"account_group": args.group, "version": version})
        return 0
    if args.accounts_command == "groups":
        _print_json(repo.list_account_groups())
        return 0
    organization = OrganizationService(repo)
    if args.accounts_command == "options":
        _print_json(organization.account_field_options_view(args.account_id))
        return 0
    if args.accounts_command == "options-set":
        _print_json(organization.set_account_field_options(args.account_id, args.kind, args.code))
        return 0
    if args.accounts_command == "columns":
        _print_json(organization.list_account_columns(args.account_id))
        return 0
    if args.accounts_command == "columns-set":
        _print_json(organization.set_account_columns(args.account_id, args.code))
        return 0
    pipeline = PipelineConfig(repo)
    if args.accounts_command == "profiles":
        _print_json(pipeline.account_profiles(args.account_id))
        return 0
    if args.accounts_command == "profiles-set":
        _print_json(pipeline.set_account_profiles(args.account_id, args.profile))
        return 0
    if args.accounts_command == "stages":
        _print_json(pipeline.stages(args.account_id))
        return 0
    if args.accounts_command == "stage-set":
        if args.enabled == args.disabled:
            raise ValueError("Set exactly one of --enabled or --disabled")
        _print_json(pipeline.set_stage(args.account_id, args.stage, args.enabled))
        return 0
    return 1


def _run(argv: list[str] | None = None) -> int:
    global _OUTPUT_MODE
    parser = build_parser()
    args = parser.parse_args(argv)
    _OUTPUT_MODE = args.render_mode or ("human" if sys.stdout.isatty() else "legacy-json")
    registry = WorkspaceRegistry(args.workspace_registry)
    if args.command == "workspaces":
        _print_json(handle_workspace_command(args, registry))
        return 0
    if args.command == "gui":
        from dagoberd.gui.workspaces import launch_workspace_gui

        if args.db and registry.read().workspaces:
            raise ValueError("Database overrides are unavailable for workspaces")
        selected = (
            resolve_workspace(args.workspace, args.config, args.db, registry=registry)
            if args.workspace or args.config or not registry.read().workspaces
            else None
        )
        return launch_workspace_gui(args, registry, selected)
    if args.command == "init":
        existing = Path(args.config or "account-eval.toml")
        if args.workspace or (
            existing.is_file() and load_config(existing).config.workspace.id is not None
        ):
            raise ValueError(
                "Use workspaces init for workspace configuration; its identity is preserved"
            )
    if args.command in {"demo", "self-test"} or (
        args.command in {"init", "agents"} and not args.workspace
    ):
        return _dispatch(args, parser)
    loaded = resolve_workspace(args.workspace, args.config, args.db, registry=registry)
    if loaded.project_path.is_file():
        args.config = str(loaded.project_path)
    exclusive = (args.command == "database" and args.database_command == "restore") or (
        args.command == "portability" and args.portability_command == "restore"
    )
    with workspace_operation(loaded, registry, exclusive=exclusive):
        if output := getattr(args, "output", None):
            registry.validate_output(loaded, output)
        return _dispatch(args, parser)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    handled = _handle_project_commands(args, parser)
    if handled is not None:
        return handled
    loaded = _loaded(args)
    if args.command == "wiso-trees":
        return _handle_wiso_tree_commands(args, loaded)
    if args.command == "portability" and args.portability_command == "restore":
        return _handle_portability_commands(args, loaded, parser)
    repo = repository(args.db, args.config)
    if args.command == "portability":
        return _handle_portability_commands(args, loaded, parser)
    handled = _handle_account_commands(args, repo)
    if handled is not None:
        return handled
    if args.command == "source-reconciliation":
        source_service = SourceReconciliationService(repo)
        if args.source_reconciliation_command == "preview":
            _print_json(source_service.preview(args.source_account, args.target_account).as_dict())
        else:
            _print_json(
                source_service.run(
                    args.source_account,
                    args.target_account,
                    args.fingerprint,
                )
            )
        return 0
    if args.command == "import":
        import_service = ImportService(
            repo,
            loaded.config.imports.mappings,
            loaded.config.imports.profiles,
            loaded.config.imports.max_file_size_mib,
        )
        if args.import_command == "preview":
            _print_json(
                import_service.preview(
                    args.source,
                    args.path,
                    args.account,
                    args.profile,
                ).as_dict()
            )
            return 0
        if args.import_command == "run":
            _print_json(import_service.execute(args.preview_id).as_dict())
            return 0
    if args.command == "comdirect":
        comdirect_connection = _comdirect_connection(loaded, args.connection)
        if args.comdirect_command == "diagnose":
            _print_json(
                ComdirectDiagnosticsService(
                    repo,
                    args.connection,
                    comdirect_connection,
                    SecretStore(),
                ).diagnose()
            )
            return 0
        if not comdirect_connection.enabled:
            raise ValueError(f"Comdirect connection '{args.connection}' is not enabled")
        if args.comdirect_command == "logout":
            _print_json(
                {
                    "connection": args.connection,
                    "logged_out": _logout_timed_comdirect_session(
                        args.connection,
                        comdirect_connection,
                    ),
                }
            )
            return 0
        result = _sync_comdirect(
            loaded,
            repo,
            args.connection,
            account_ids=args.accounts,
            booking_from=args.booking_from,
            booking_to=args.booking_to,
            resume=args.resume,
            force_new_auth=True,
        )
        _print_json({"recovered": True, "sync": asdict(result)})
        return 0
    if args.command == "sync" and args.provider == "comdirect":
        result = _sync_comdirect(
            loaded,
            repo,
            args.connection,
            account_ids=args.accounts,
            booking_from=args.booking_from,
            booking_to=args.booking_to,
            resume=args.resume,
        )
        _print_json(asdict(result))
        return 0
    if args.command == "transaction-note":
        _print_json(
            OrganizationService(repo).set_transaction_note(
                args.transaction_id,
                args.text,
                expected_version=args.version,
            )
        )
        return 0
    if args.command == "transactions":
        scope = _account_scope(repo, args.accounts, args.group)
        statuses = None if args.all_statuses else tuple(args.status or ("pending", "booked"))
        _print_json(
            [
                safe_financial_record(item)
                for item in repo.list_transactions(
                    args.limit,
                    args.booking_from,
                    args.booking_to,
                    account_scope=scope,
                    booking_statuses=statuses,
                )
            ]
        )
        return 0
    organization = OrganizationService(repo)
    if args.command == "manual" and args.manual_command == "add":
        _print_json(
            organization.create_manual_transaction(
                args.account_id,
                booking_date=date.fromisoformat(args.booking_date),
                valuta_date=(date.fromisoformat(args.valuta_date) if args.valuta_date else None),
                amount=Decimal(args.amount),
                purpose=args.purpose,
                counterparty=args.counterparty,
                reference=args.reference,
                booking_status=args.status,
                idempotency_key=args.idempotency_key,
                category_profile=args.profile,
                category_code=args.category,
                tags=tuple(args.tag),
                dimensions=_dimension_map(args.dimension),
            )
        )
        return 0
    if args.command == "manual" and args.manual_command == "update":
        _print_json(
            organization.update_manual_transaction(
                args.transaction_id,
                expected_version=args.version,
                booking_date=date.fromisoformat(args.booking_date) if args.booking_date else None,
                valuta_date=date.fromisoformat(args.valuta_date) if args.valuta_date else None,
                amount=Decimal(args.amount) if args.amount else None,
                purpose=args.purpose,
                counterparty=args.counterparty,
                reference=args.reference,
            )
        )
        return 0
    if args.command == "search":
        _print_json(
            [
                safe_financial_record(item)
                for item in organization.search_transactions(
                    TransactionSearchFilter(
                        text=args.text,
                        booking_from=args.booking_from,
                        booking_to=args.booking_to,
                        amount_min_minor=(
                            to_minor_units(Decimal(args.amount_min))
                            if args.amount_min is not None
                            else None
                        ),
                        amount_max_minor=(
                            to_minor_units(Decimal(args.amount_max))
                            if args.amount_max is not None
                            else None
                        ),
                        currencies=tuple(args.currency),
                        booking_statuses=tuple(args.status),
                        category_codes=tuple(args.category),
                        classification_sources=tuple(args.classification_source),
                        tags=tuple(args.tag),
                        dimensions=_dimension_filters(args.dimension),
                        merchant=args.merchant,
                        document_status=args.document_status,
                        only_unreviewed=args.unreviewed,
                    ),
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    limit=args.limit,
                    offset=args.offset,
                )
            ]
        )
        return 0
    if args.command == "tags":
        if args.tags_command == "create":
            _print_json(organization.create_tag(args.name))
        elif args.tags_command == "list":
            _print_json(organization.list_tags())
        else:
            _print_json(
                organization.tag_transaction(
                    args.transaction_id,
                    args.tag,
                    expected_version=args.version,
                    remove=args.tags_command == "remove",
                )
            )
        return 0
    if args.command == "dimensions":
        if args.dimensions_command == "create":
            _print_json(organization.create_dimension(args.code, args.name))
        elif args.dimensions_command == "value-add":
            _print_json(organization.create_dimension_value(args.dimension, args.code, args.name))
        elif args.dimensions_command == "list":
            _print_json(organization.list_dimensions())
        elif args.dimensions_command == "rename":
            _print_json(
                organization.update_dimension(
                    args.code, name=args.name, expected_version=args.version
                )
            )
        elif args.dimensions_command == "deactivate":
            _print_json(
                organization.update_dimension(
                    args.code, active=False, expected_version=args.version
                )
            )
        elif args.dimensions_command == "delete":
            organization.delete_dimension(args.code)
            _print_json({"code": args.code, "deleted": True})
        elif args.dimensions_command == "value-rename":
            _print_json(
                organization.update_dimension_value(
                    args.dimension, args.code, name=args.name, expected_version=args.version
                )
            )
        elif args.dimensions_command == "value-deactivate":
            _print_json(
                organization.update_dimension_value(
                    args.dimension, args.code, active=False, expected_version=args.version
                )
            )
        elif args.dimensions_command == "value-delete":
            organization.delete_dimension_value(args.dimension, args.code)
            _print_json({"dimension": args.dimension, "code": args.code, "deleted": True})
        else:
            _print_json(
                organization.set_transaction_dimension(
                    args.transaction_id,
                    args.dimension,
                    args.value if args.dimensions_command == "set" else None,
                    expected_version=args.version,
                )
            )
        return 0
    if args.command == "merchants":
        if args.merchants_command == "create":
            _print_json(organization.create_merchant(args.name))
        elif args.merchants_command == "alias-add":
            _print_json(organization.add_merchant_alias(args.merchant, args.alias))
        elif args.merchants_command == "list":
            _print_json(organization.list_merchants())
        else:
            _print_json(
                organization.set_transaction_merchant(
                    args.transaction_id,
                    args.merchant,
                    expected_version=args.version,
                )
            )
        return 0
    if args.command == "review":
        if args.review_command == "list":
            _print_json(
                organization.list_review_items(
                    status=args.status,
                    item_type=args.item_type,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    limit=args.limit,
                )
            )
        else:
            _print_json(
                organization.review_item(
                    args.review_id,
                    args.review_command,
                    expected_version=args.version,
                )
            )
        return 0
    if args.command == "bulk":
        if args.bulk_command == "preview":
            _print_json(
                organization.create_bulk_preview(
                    args.action,
                    args.transaction_ids,
                    _bulk_payload(args),
                )
            )
        else:
            _print_json(
                organization.execute_bulk_preview(args.preview_id, fingerprint=args.fingerprint)
            )
        return 0
    analysis_service = AnalysisService(repo)
    if args.command == "rates":
        if args.rates_command == "add":
            _print_json(
                analysis_service.add_exchange_rate(
                    args.source_currency,
                    args.target_currency,
                    Decimal(args.rate),
                    rate_date=date.fromisoformat(args.date),
                    source=args.source,
                    source_version=args.source_version,
                    idempotency_key=args.idempotency_key,
                )
            )
        else:
            _print_json(
                analysis_service.list_exchange_rates(
                    source_currency=args.source_currency,
                    target_currency=args.target_currency,
                    limit=args.limit,
                )
            )
        return 0
    if args.command == "analyses":
        if args.analyses_command == "create":
            _print_json(
                analysis_service.create_saved_analysis(
                    args.code,
                    args.name,
                    _analysis_definition(args, repo, loaded),
                    description=args.description,
                )
            )
        elif args.analyses_command == "update":
            definition = AnalysisDefinition.model_validate_json(
                Path(args.definition).read_text(encoding="utf-8")
            )
            _print_json(
                analysis_service.update_saved_analysis(
                    args.analysis,
                    definition,
                    expected_version=args.version,
                    name=args.name,
                    description=args.description,
                )
            )
        elif args.analyses_command == "list":
            _print_json(analysis_service.list_saved_analyses())
        elif args.analyses_command == "show":
            _print_json(analysis_service.get_saved_analysis(args.analysis))
        else:
            _print_json(
                analysis_service.run_saved_analysis(
                    args.analysis,
                    args.output or loaded.config.reporting.output_dir,
                    as_of=date.fromisoformat(args.as_of) if args.as_of else None,
                    formats=tuple(args.formats) if args.formats else None,
                )
            )
        return 0
    if args.command == "transaction-status":
        version = repo.transition_transaction_status(
            args.transaction_id,
            args.status,
            args.version,
            reason=args.reason,
            source="cli",
        )
        _print_json(
            {
                "transaction_id": args.transaction_id,
                "booking_status": args.status,
                "version": version,
            }
        )
        return 0
    if args.command == "transaction-history":
        _print_json(repo.list_transaction_status_history(args.transaction_id))
        return 0
    if args.command == "relations":
        if args.relations_command == "suggest":
            config = loaded.config.transaction_semantics
            semantics_service = TransactionSemanticsService(
                repo,
                transfer_max_days=config.transfer_max_days,
                transfer_max_fee_minor=config.transfer_max_fee_minor,
                refund_max_days=config.refund_max_days,
                correction_max_days=config.correction_max_days,
                minimum_score=config.minimum_candidate_score,
            )
            _print_json(
                semantics_service.suggest(
                    args.relation_type,
                    transaction_id=args.transaction,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    limit=args.limit,
                )
            )
            return 0
        if args.relations_command == "list":
            _print_json(
                repo.list_transaction_relations(
                    relation_type=args.relation_type,
                    status=args.status,
                    transaction_id=args.transaction,
                    limit=args.limit,
                )
            )
            return 0
        if args.relations_command == "create":
            _print_json(
                repo.create_transaction_relation(
                    args.relation_type,
                    args.source_transaction_id,
                    args.target_transaction_id,
                    expected_source_version=args.source_version,
                    expected_target_version=args.target_version,
                    fee_amount_minor=to_minor_units(Decimal(args.fee)),
                    reasons=args.reason,
                    metadata={"created_by": "cli"},
                )
            )
            return 0
        _print_json(
            repo.review_transaction_relation(
                args.relation_id,
                args.relations_command,
                args.version,
            )
        )
        return 0
    if args.command == "balances":
        if args.balances_command == "add":
            _print_json(
                repo.record_balance_snapshot(
                    args.account_id,
                    to_minor_units(Decimal(args.amount)),
                    balance_date=args.date,
                    source=args.source,
                    external_id=args.external_id,
                    expected_account_version=args.version,
                )
            )
            return 0
        if args.balances_command == "list":
            _print_json(
                repo.list_balance_snapshots(
                    account_id=args.account,
                    limit=args.limit,
                )
            )
            return 0
        if args.balances_command == "reconcile":
            _print_json(
                repo.reconcile_balance_snapshot(
                    args.snapshot_id,
                    refresh=True,
                )
            )
            return 0
        _print_json(
            repo.list_reconciliations(
                account_id=args.account,
                limit=args.limit,
            )
        )
        return 0
    if args.command == "classifications" and args.classification_command == "set":
        print(
            repo.classify(
                args.transaction_id,
                args.profile,
                args.category,
                "manual",
                1.0,
                "CLI",
                "suggested" if args.suggest else "applied",
                args.version,
            )
        )
        return 0
    if args.command == "classifications" and args.classification_command == "apply-rules":
        print(repo.apply_rules(args.transaction_id, args.profile) or "No rule matched")
        return 0
    if args.command == "classifications" and args.classification_command == "deterministic":
        print(
            ClassificationService(repo).classify(
                args.transaction_id,
                args.profile,
            )
            or "No automatic classification"
        )
        return 0
    if args.command == "classification":
        provider = create_llm_provider(
            loaded.config,
            SecretStore(dotenv_path=loaded.project_path.with_name(".env")),
        )
        diagnostic_service = ClassificationService(
            repo,
            provider,
            loaded.config.privacy,
            loaded.config.llm.model,
        )
        if args.classification_diagnostic_command == "explain":
            _print_json(diagnostic_service.explain(args.transaction_id, args.profile))
        else:
            _print_json(
                diagnostic_service.test_rule_pattern(
                    args.pattern,
                    profile_code=args.profile,
                    account_id=args.account,
                    limit=args.limit,
                )
            )
        return 0
    if args.command == "classifications":
        if args.classification_command == "review":
            _print_json(
                repo.list_classifications(
                    status=args.status,
                    profile_code=args.profile,
                    limit=args.limit,
                )
            )
            return 0
        if args.classification_command in {"accept", "reject"}:
            _print_json(
                repo.review_classification(
                    args.classification_id,
                    args.classification_command,
                    args.version,
                )
            )
            return 0
        llm_preview_service = LLMPreviewService(
            repo,
            None,
            loaded.config.llm,
            loaded.config.privacy,
        )
        if args.classification_command == "preview":
            _print_json(
                llm_preview_service.create(
                    args.profile,
                    transaction_ids=args.transaction,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    limit=args.limit,
                )
            )
            return 0
        if args.preview:
            if not args.fingerprint:
                raise ValueError("--fingerprint is required with --preview")
            preview_service = LLMPreviewService(
                repo,
                create_llm_provider(
                    loaded.config,
                    SecretStore(dotenv_path=loaded.project_path.with_name(".env")),
                ),
                loaded.config.llm,
                loaded.config.privacy,
            )
            _print_json(preview_service.execute(args.preview, args.fingerprint))
            return 0
        if loaded.config.llm.enabled and loaded.config.llm.mode == "cloud":
            raise ValueError(
                "Cloud LLM classification requires 'classifications preview' followed by "
                "'classifications run --preview ... --fingerprint ...'"
            )
        classification_service = ClassificationService(
            repo,
            create_llm_provider(
                loaded.config,
                SecretStore(dotenv_path=loaded.project_path.with_name(".env")),
            ),
            loaded.config.privacy,
            loaded.config.llm.model,
        )
        if args.transaction:
            _print_json(
                {
                    "transaction_id": args.transaction,
                    "classification_id": classification_service.classify(
                        args.transaction, args.profile
                    ),
                }
            )
        else:
            _print_json(
                classification_service.classify_open(
                    args.profile,
                    limit=args.limit,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                )
            )
        return 0
    if args.command == "profiles":
        print(repo.ensure_profile(args.code, args.name, args.description))
        return 0
    if args.command == "rules" and args.rules_command == "add":
        print(
            repo.add_rule(
                args.profile,
                args.name,
                args.pattern,
                args.category,
                args.priority,
                args.account,
            )
        )
        return 0
    if args.command == "hints" and args.hints_command == "add":
        print(
            repo.add_hint(
                args.profile,
                args.text,
                pattern=args.pattern,
                category_code=args.category,
                priority=args.priority,
                account_id=args.account,
            )
        )
        return 0
    if args.command == "rules":
        if args.rules_command == "list":
            _print_json(repo.list_rules(profile_code=args.profile, account_id=args.account))
        elif args.rules_command == "update":
            _print_json(
                repo.update_rule(
                    args.rule_id,
                    expected_version=args.version,
                    name=args.name,
                    pattern=args.pattern,
                    category_code=args.category,
                    priority=args.priority,
                    active=args.active,
                    account_id=args.account,
                )
            )
        else:
            repo.delete_rule(args.rule_id, args.version)
            _print_json({"id": args.rule_id, "deleted": True})
        return 0
    if args.command == "hints":
        if args.hints_command == "list":
            _print_json(repo.list_hints(profile_code=args.profile, account_id=args.account))
        elif args.hints_command == "update":
            _print_json(
                repo.update_hint(
                    args.hint_id,
                    expected_version=args.version,
                    text=args.text,
                    pattern=args.pattern,
                    category_code=args.category,
                    priority=args.priority,
                    active=args.active,
                    account_id=args.account,
                )
            )
        else:
            repo.delete_hint(args.hint_id, args.version)
            _print_json({"id": args.hint_id, "deleted": True})
        return 0
    if args.command == "report":
        output = args.output or str(loaded.config.reporting.output_dir)
        scope = _account_scope(repo, args.accounts, args.group)
        formats = tuple(args.formats or loaded.config.reporting.default_formats)
        for path in LedgerReportGenerator(
            repo, output, extensions=loaded.config.reporting.extensions
        ).generate(
            args.profile,
            formats,
            account_scope=scope,
            booking_from=args.booking_from,
            booking_to=args.booking_to,
            currency=args.currency,
            extension=args.extension,
        ):
            print(path)
        return 0
    if args.command == "deliveries":
        delivery_service = ReportDeliveryService(
            repo,
            loaded.config.delivery,
            SecretStore(dotenv_path=loaded.project_path.with_name(".env")),
        )
        if args.delivery_command == "list":
            _print_json(delivery_service.list_report_runs(args.limit))
            return 0
        if args.delivery_command == "preview":
            _print_json(delivery_service.preview(args.target, args.report_run))
            return 0
        _print_json(
            delivery_service.send(
                args.preview_id,
                args.fingerprint,
                idempotency_key=args.idempotency_key,
            )
        )
        return 0
    if args.command == "documents" and args.documents_command == "register":
        amount = Decimal(args.amount) if args.amount else None
        print(
            DocumentMatcher(repo).add_document(
                args.filename,
                amount,
                merchant=args.merchant,
            )
        )
        return 0
    if args.command == "documents":
        matcher = _document_matcher(repo, loaded)
        if args.documents_command == "preview":
            _print_json(_document_batch_service(repo, loaded).preview(args.path).as_dict())
            return 0
        if args.documents_command == "run":
            _print_json(
                _document_batch_service(repo, loaded)
                .execute(args.preview_id, args.fingerprint)
                .as_dict()
            )
            return 0
        if args.documents_command == "import":
            _print_json(
                matcher.import_file(
                    args.path,
                    amount=Decimal(args.amount) if args.amount else None,
                    document_date=(date.fromisoformat(args.date) if args.date else None),
                    merchant=args.merchant,
                    reference=args.reference,
                    account_id=args.account,
                    profile_code=args.profile,
                )
            )
            return 0
        if args.documents_command == "list":
            _print_json(matcher.list_documents(status=args.status))
            return 0
        if args.documents_command == "candidates":
            _print_json(
                matcher.suggest(
                    args.document_id,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    profile_code=args.profile,
                )
            )
            return 0
        if args.documents_command == "preview-matches":
            _print_json(
                matcher.preview_matches(
                    args.document_id,
                    account_scope=_account_scope(repo, args.accounts, args.group),
                    profile_code=args.profile,
                    limit=args.limit,
                )
            )
            return 0
        if args.documents_command == "apply-matches":
            _print_json(
                matcher.apply_match_preview(
                    args.document_id,
                    args.fingerprint,
                    args.transaction,
                    expected_document_version=args.document_version,
                    append=args.append,
                )
            )
            return 0
        if args.documents_command in {"accept", "reject"}:
            _print_json(matcher.review(args.match_id, args.documents_command, args.version))
            return 0
        if args.documents_command == "unmatch":
            _print_json(matcher.unmatch(args.match_id, args.version))
            return 0
        if args.documents_command == "assign":
            _print_json(
                matcher.assign(
                    args.document_id,
                    args.transaction_id,
                    expected_document_version=args.document_version,
                    expected_transaction_version=args.transaction_version,
                    append=args.append,
                )
            )
            return 0
    if args.command == "splits":
        if args.splits_command == "revert":
            repo.revert_split(args.transaction_id, args.version)
            _print_json({"transaction_id": args.transaction_id, "status": "active"})
            return 0
        parts: list[tuple[object, str | None, str | None]] = []
        for raw in args.part:
            values = raw.split(":", 2)
            if len(values) < 2:
                parser.error("--part expects Amount:Category[:Note]")
            parts.append(
                (
                    Decimal(values[0]),
                    values[1] or None,
                    values[2] if len(values) == 3 else None,
                )
            )
        operation = (
            repo.replace_split if args.splits_command == "update" else repo.split_transaction
        )
        _print_json(
            {"split_ids": operation(args.transaction_id, args.profile, parts, args.version)}
        )
        return 0
    if args.command == "audit":
        if args.limit < 1 or args.limit > 500:
            parser.error("--limit must be between 1 and 500")
        query = "SELECT * FROM ledger_audit_events"
        parameters: list[object] = []
        if args.entity_type:
            query += " WHERE entity_type=?"
            parameters.append(args.entity_type)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        parameters.append(args.limit)
        with repo.database.connect() as connection:
            _print_json([dict(row) for row in connection.execute(query, parameters)])
        return 0
    return 1


def _account_scope(
    repo: LedgerRepository,
    account_ids: list[str] | None,
    group: str | None,
) -> AccountScope:
    if account_ids and group:
        raise ValueError("Use either --accounts or --group")
    if group:
        return repo.account_scope_for_group(group)
    if account_ids:
        return AccountScope.selected(account_ids)
    return AccountScope.all()


def _dimension_filters(raw_filters: list[str]) -> dict[str, tuple[str, ...]]:
    filters: dict[str, tuple[str, ...]] = {}
    for raw in raw_filters:
        if "=" not in raw:
            raise ValueError("--dimension expects Dimension=Value1,Value2")
        code, values = raw.split("=", 1)
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("Dimension must not be empty")
        filters[normalized_code] = tuple(
            value.strip() for value in values.split(",") if value.strip()
        )
    return filters


def _dimension_map(raw_filters: list[str]) -> dict[str, str]:
    return {code: values[0] for code, values in _dimension_filters(raw_filters).items()}


def _bulk_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "edit_fields":
        if not args.patch_json:
            raise ValueError("edit_fields requires --patch-json")
        parsed = json.loads(args.patch_json)
        if not isinstance(parsed, dict):
            raise ValueError("--patch-json must contain a JSON object")
        return parsed
    payload = {
        key: value
        for key in ("profile", "category", "tag", "dimension", "value", "merchant")
        if (value := getattr(args, key, None)) is not None
    }
    return payload


def _analysis_definition(
    args: argparse.Namespace,
    repo: LedgerRepository,
    loaded: LoadedConfig,
) -> AnalysisDefinition:
    statuses = tuple(args.status) if args.status else ("pending", "booked")
    return AnalysisDefinition(
        account_scope=_account_scope(repo, args.accounts, args.group),
        profile=args.profile,
        booking_from=args.booking_from,
        booking_to=args.booking_to,
        relative_months=args.relative_months,
        text=args.text,
        amount_min_minor=(
            to_minor_units(Decimal(args.amount_min)) if args.amount_min is not None else None
        ),
        amount_max_minor=(
            to_minor_units(Decimal(args.amount_max)) if args.amount_max is not None else None
        ),
        currencies=tuple(args.currency),
        booking_statuses=statuses,
        category_codes=tuple(args.category),
        tags=tuple(args.tag),
        dimensions=_dimension_filters(args.dimension),
        merchant=args.merchant,
        document_status=args.document_status,
        base_currency=(args.base_currency or loaded.config.reporting.base_currency),
        fixed_cost_tag=args.fixed_cost_tag,
        stale_balance_days=args.stale_balance_days,
        sections=tuple(args.sections)
        if args.sections
        else (
            "cashflow",
            "income_expense",
            "fixed_costs",
            "trends",
            "net_worth",
        ),
        formats=tuple(args.formats) if args.formats else (loaded.config.reporting.default_formats),
        group_by=tuple(args.group_by),
    )


def _document_matcher(repo: LedgerRepository, loaded: LoadedConfig) -> DocumentMatcher:
    config = loaded.config.documents
    return DocumentMatcher(
        repo,
        config.storage_dir,
        auto_match_threshold=config.auto_match_threshold,
        auto_match_minimum_gap=config.auto_match_minimum_gap,
        extract_pdf_text=config.extract_pdf_text,
    )


def _document_batch_service(repo: LedgerRepository, loaded: LoadedConfig) -> DocumentBatchService:
    config = loaded.config.documents
    return DocumentBatchService(
        repo,
        config.storage_dir,
        max_file_size_bytes=loaded.config.imports.max_file_size_mib * 1024 * 1024,
        ocr_provider=create_local_ocr_provider(
            enabled=config.local_ocr_enabled,
            executable=config.local_ocr_executable,
            pdf_renderer=config.local_ocr_pdf_renderer,
            languages=config.local_ocr_languages,
            timeout_seconds=config.local_ocr_timeout_seconds,
        ),
    )


def _sync_comdirect(
    loaded: LoadedConfig,
    repo: LedgerRepository,
    connection_name: str,
    *,
    account_ids: list[str] | None = None,
    booking_from: str | None = None,
    booking_to: str | None = None,
    resume: str | None = None,
    force_new_auth: bool = False,
) -> SyncResult:
    connection = _comdirect_connection(loaded, connection_name)
    if not connection.enabled:
        raise ValueError(f"Comdirect connection '{connection_name}' is not enabled")
    if connection.session_mode == "ephemeral":
        provider, _ = _new_comdirect_provider(connection_name, connection)
        provider.login()
        try:
            return _run_comdirect_sync(
                repo,
                provider,
                connection_name=connection_name,
                account_ids=account_ids,
                booking_from=booking_from,
                booking_to=booking_to,
                resume=resume,
            )
        finally:
            provider.revoke()

    state = (
        _authenticate_comdirect(connection_name, connection)
        if force_new_auth
        else _load_timed_comdirect_session(connection_name, connection)
    )
    if force_new_auth:
        _store_timed_comdirect_session(connection_name, connection, state)
    provider, _ = _new_comdirect_provider(connection_name, connection)
    provider.restore_session(state)
    result = _run_comdirect_sync(
        repo,
        provider,
        connection_name=connection_name,
        account_ids=account_ids,
        booking_from=booking_from,
        booking_to=booking_to,
        resume=resume,
    )
    _store_timed_comdirect_session(connection_name, connection, provider.export_session())
    return result


def _comdirect_connection(
    loaded: LoadedConfig,
    connection_name: str,
) -> ComdirectConnectionConfig:
    return loaded.config.comdirect.connection(connection_name)


def _new_comdirect_provider(
    connection_name: str,
    connection: ComdirectConnectionConfig,
) -> tuple[ComdirectProvider, SecretStore]:
    secrets = SecretStore()
    challenge = LocalCLIChallengeHandler()
    provider = ComdirectProvider.from_config(
        connection,
        secrets,
        getpass.getpass(f"Comdirect PIN for {connection_name}: "),
        push_tan_callback=challenge.push_tan,
        photo_tan_callback=challenge.photo_tan,
        sms_tan_callback=challenge.sms_tan,
    )
    return provider, secrets


def _authenticate_comdirect(
    connection_name: str,
    connection: ComdirectConnectionConfig,
) -> ComdirectSessionState:
    _logout_timed_comdirect_session(connection_name, connection)
    provider, _ = _new_comdirect_provider(connection_name, connection)
    return provider.login()


def _run_comdirect_sync(
    repo: LedgerRepository,
    provider: ComdirectProvider,
    *,
    connection_name: str,
    account_ids: list[str] | None,
    booking_from: str | None,
    booking_to: str | None,
    resume: str | None,
) -> SyncResult:
    return ComdirectSyncService(repo, provider, connection_name).synchronize(
        account_ids=account_ids,
        booking_from=(date.fromisoformat(booking_from) if booking_from else None),
        booking_to=date.fromisoformat(booking_to) if booking_to else None,
        resume_sync_run_id=resume,
        interactive=False,
    )


def _timed_session_expiry(state: ComdirectSessionState, maximum_age_hours: int) -> datetime:
    authenticated_at = datetime.fromisoformat(state.authenticated_at)
    if authenticated_at.tzinfo is None:
        authenticated_at = authenticated_at.replace(tzinfo=UTC)
    return authenticated_at + timedelta(hours=maximum_age_hours)


def _load_timed_comdirect_session(
    connection_name: str,
    connection: ComdirectConnectionConfig,
) -> ComdirectSessionState:
    secrets = SecretStore()
    serialized = secrets.get_keyring(comdirect_timed_session_secret(connection_name))
    if not serialized:
        raise ValueError(
            f"Comdirect authentication required; run local auth comdirect {connection_name}"
        )
    try:
        state = ComdirectSessionState.from_json(serialized)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "Invalid or expired Comdirect session; authenticate locally again"
        ) from error
    if state.is_within(connection.keychain_session_max_age_hours):
        return state
    _logout_timed_comdirect_session(connection_name, connection, state=state)
    raise ValueError(f"Comdirect session expired; run local auth comdirect {connection_name}")


def _store_timed_comdirect_session(
    connection_name: str,
    connection: ComdirectConnectionConfig,
    state: ComdirectSessionState,
) -> None:
    if not state.is_within(connection.keychain_session_max_age_hours):
        raise ValueError("The Comdirect session has expired and will not be stored")
    SecretStore().set(comdirect_timed_session_secret(connection_name), state.to_json())


def _logout_timed_comdirect_session(
    connection_name: str,
    connection: ComdirectConnectionConfig,
    *,
    state: ComdirectSessionState | None = None,
) -> bool:
    secrets = SecretStore()
    session_secret = comdirect_timed_session_secret(connection_name)
    if state is None:
        serialized = secrets.get_keyring(session_secret)
        if not serialized:
            return False
        try:
            state = ComdirectSessionState.from_json(serialized)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            secrets.delete(session_secret)
            return True
    provider = ComdirectProvider(timeout_seconds=connection.timeout_seconds)
    provider.restore_session(state)
    provider.revoke()
    secrets.delete(session_secret)
    return True


def _portability_service(loaded: LoadedConfig) -> PortabilityService:
    return PortabilityService(
        loaded.config.database.path,
        loaded.config.documents.storage_dir,
        loaded.config.reporting.output_dir,
        loaded.project_path,
        masked_config=masked_config(loaded.config),
    )


def _required_secret(loaded: LoadedConfig, name: str) -> str:
    value = SecretStore(dotenv_path=loaded.project_path.with_name(".env")).get(name)
    if not value:
        raise ValueError(f"Missing secret {name}; store it locally with config set-secret")
    return value


def _completion_script(shell: str) -> str:
    command_action = next(action for action in build_parser()._actions if action.dest == "command")
    commands = " ".join(getattr(command_action, "choices", {}))
    if shell == "fish":
        return f"complete -c dagoberd -f -a '{commands}'"
    if shell == "powershell":
        return (
            "Register-ArgumentCompleter -Native -CommandName dagoberd "
            "-ScriptBlock { param($wordToComplete) @('"
            + commands.replace(" ", "','")
            + '\') | Where-Object { $_ -like "$wordToComplete*" } }'
        )
    return f"complete -W '{commands}' dagoberd"


def _handle_wiso_tree_commands(args: argparse.Namespace, loaded: LoadedConfig) -> int:
    free_trees = {
        field: path
        for field, path in (
            ("Frei 1", args.free_1),
            ("Frei 2", args.free_2),
            ("Frei 3", args.free_3),
        )
        if path is not None
    }
    mapping_file = loaded.config.imports.wiso_trees.mapping_file
    plan = preview_wiso_tree_taxonomy(
        args.categories, args.tax, free_trees, mapping_file=mapping_file
    )
    if args.wiso_tree_command == "preview":
        _print_json(plan.as_dict())
        return 0
    if plan.fingerprint != args.fingerprint:
        raise ValueError("WISO tree fingerprint does not match the current inputs and mapping")
    repo = repository(args.db, args.config)
    _print_json(
        import_wiso_tree_taxonomy(
            repo, args.categories, args.tax, free_trees, args.fingerprint, mapping_file=mapping_file
        )
    )
    return 0


def _handle_portability_commands(
    args: argparse.Namespace, loaded: LoadedConfig, parser: argparse.ArgumentParser
) -> int:
    service = _portability_service(loaded)
    command = args.portability_command
    password: str | None
    if command == "export":
        _print_json(service.export(args.output))
    elif command == "backup":
        password = _required_secret(loaded, args.password_secret)
        _print_json(service.encrypted_backup(args.output, password))
    elif command == "verify":
        password = _required_secret(loaded, args.password_secret) if args.password_secret else None
        _print_json(service.verify(args.path, password=password))
    elif command == "restore":
        if not args.yes:
            parser.error("portability restore requires --yes")
        password = _required_secret(loaded, args.password_secret) if args.password_secret else None
        _print_json(service.restore(args.path, password=password))
    elif command == "integrity":
        result = service.integrity()
        _print_json(result)
        return 0 if result["valid"] else 1
    elif command == "delete-preview":
        _print_json(service.preview_deletion(args.scope).as_dict())
    elif command == "delete-run":
        if not args.yes:
            parser.error("portability delete-run requires --yes")
        _print_json(service.execute_deletion(args.preview_id, args.fingerprint))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with stable machine-readable errors and exit codes."""
    try:
        return _run(argv)
    except RestoreError as error:
        return _render_failure(
            error.code,
            str(error),
            ExitCode.OPERATION_FAILED
            if error.rollback_complete or error.restore_complete
            else ExitCode.INTEGRITY,
            details=error.details,
        )
    except VersionConflictError as error:
        return _render_failure("version_conflict", str(error), ExitCode.VERSION_CONFLICT)
    except LLMProviderError as error:
        return _render_failure("llm_failed", str(error), ExitCode.EXTERNAL_SERVICE)
    except FileNotFoundError as error:
        return _render_failure("not_found", str(error), ExitCode.CONFIGURATION)
    except FileExistsError as error:
        return _render_failure("already_exists", str(error), ExitCode.OPERATION_FAILED)
    except (KeyError, ValueError) as error:
        return _render_failure("invalid_request", str(error), ExitCode.OPERATION_FAILED)
    except Exception as error:  # console boundary: no traceback or secret-bearing response bodies
        return _render_failure(
            "operation_failed",
            type(error).__name__,
            ExitCode.OPERATION_FAILED,
        )


def _render_failure(code: str, message: str, exit_code: ExitCode, *, details: Any = None) -> int:
    payload = failure(code, message, details=details)
    if _OUTPUT_MODE in {"json", "legacy-json"}:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(f"Error [{code}]: {message}", file=sys.stderr)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
