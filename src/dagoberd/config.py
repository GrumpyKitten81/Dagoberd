"""Typed, local project configuration for Dagoberd."""

from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dagoberd.date_display import DisplayConfig
from dagoberd.llm_endpoint import validate_llm_endpoint

PROJECT_CONFIG_NAME = "dagoberd.toml"
LOCAL_CONFIG_NAME = "dagoberd.local.toml"
DATA_ROOT_ENV = "DAGOBERD_DATA_ROOT"
CLOUD_WORKSPACE_ENV = "DAGOBERD_CLOUD_WORKSPACE"
DEFAULT_DATABASE_PATH = Path("output/dagoberd.db")
DEFAULT_DOCUMENTS_DIR = Path("output/documents")
DEFAULT_REPORTING_DIR = Path("output")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class DataConfig(StrictModel):
    root: Path | None = None


class CloudConfig(StrictModel):
    workspace: Path | None = None
    root: Path | None = None

    @model_validator(mode="after")
    def validate_roots(self) -> CloudConfig:
        if self.root is not None and self.workspace is not None:
            raise ValueError("Use either cloud.root or cloud.workspace")
        return self


class WorkspaceConfig(StrictModel):
    id: UUID | None = None
    name: str = ""


class DatabaseConfig(StrictModel):
    path: Path = DEFAULT_DATABASE_PATH


class ComdirectConnectionConfig(StrictModel):
    enabled: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    username_secret: str
    client_id_secret: str
    client_secret_secret: str
    session_mode: Literal["ephemeral", "keychain_timed"] = "ephemeral"
    keychain_session_max_age_hours: int = Field(default=12, ge=1, le=12)
    allow_mcp_sync: bool = False

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_secret_references(cls, value: Any) -> Any:
        if isinstance(value, dict) and {"password_secret", "session_secret"} & value.keys():
            raise ValueError(
                "Comdirect PIN and sessions are not configurable secrets; "
                "use the local PIN prompt and the timed keychain session"
            )
        return value

    @model_validator(mode="after")
    def validate_mcp_sync(self) -> ComdirectConnectionConfig:
        if self.allow_mcp_sync and self.session_mode != "keychain_timed":
            raise ValueError("comdirect connection allow_mcp_sync requires keychain_timed")
        return self


class ComdirectConfig(StrictModel):
    connections: dict[str, ComdirectConnectionConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_named_connections(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, dict):
            return value
        legacy_fields = {
            "enabled",
            "timeout_seconds",
            "username_secret",
            "client_id_secret",
            "client_secret_secret",
            "session_mode",
            "keychain_session_max_age_hours",
            "allow_mcp_sync",
            "password_secret",
            "session_secret",
        }
        if legacy_fields & value.keys():
            raise ValueError(
                "The legacy [comdirect] configuration is not supported; "
                "configure [comdirect.connections.<name>] instead"
            )
        connections = value.get("connections", {})
        if not isinstance(connections, dict):
            return value
        normalized: dict[str, Any] = {}
        for name, connection in connections.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
                raise ValueError(
                    "Comdirect connection names must be lowercase slugs with up to 32 characters"
                )
            if not isinstance(connection, dict):
                normalized[name] = connection
                continue
            configured = dict(connection)
            prefix = f"comdirect.{name}"
            expected_secrets = {
                "username_secret": f"{prefix}.username",
                "client_id_secret": f"{prefix}.client-id",
                "client_secret_secret": f"{prefix}.client-secret",
            }
            for field, expected in expected_secrets.items():
                if field in configured and configured[field] != expected:
                    raise ValueError(
                        f"{field} for Comdirect connection '{name}' must be '{expected}'"
                    )
                configured[field] = expected
            normalized[name] = configured
        return {"connections": normalized}

    def connection(self, name: str) -> ComdirectConnectionConfig:
        try:
            return self.connections[name]
        except KeyError as error:
            configured = ", ".join(sorted(self.connections)) or "none"
            raise ValueError(
                f"Unknown Comdirect connection '{name}'. Configured connections: {configured}"
            ) from error

    @model_validator(mode="after")
    def validate_isolated_secret_references(self) -> ComdirectConfig:
        references: dict[str, str] = {}
        for name, connection in self.connections.items():
            for secret_name in (
                connection.username_secret,
                connection.client_id_secret,
                connection.client_secret_secret,
            ):
                if owner := references.get(secret_name):
                    raise ValueError(
                        f"Comdirect connections '{owner}' and '{name}' reuse secret "
                        f"reference '{secret_name}'"
                    )
                references[secret_name] = name
        return self


def comdirect_timed_session_secret(connection_name: str) -> str:
    return f"comdirect.{connection_name}.timed-session"


class LLMConfig(StrictModel):
    enabled: bool = False
    mode: Literal["local", "cloud"] = "local"
    provider: str = "openai-compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = ""
    api_key_secret: str = "DAGOBERD_LLM_API_KEY"
    allow_cloud: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int = Field(default=512, ge=64, le=8192)
    batch_size: int = Field(default=10, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def validate_activation(self) -> LLMConfig:
        if self.enabled and not self.model.strip():
            raise ValueError("llm.model is required when the LLM is enabled")
        if self.enabled and self.mode == "cloud" and not self.allow_cloud:
            raise ValueError("Cloud LLM use requires llm.allow_cloud = true")
        if self.enabled:
            validate_llm_endpoint(self.base_url, self.mode)
        return self


class PrivacyConfig(StrictModel):
    mask_visible_characters: int = Field(default=4, ge=0, le=8)
    max_text_length: int = Field(default=500, ge=50, le=5000)
    allow_document_content_for_llm: bool = False


class ReportExtensionConfig(StrictModel):
    path: Path
    profile: str = Field(min_length=1)
    include_history: bool = False


class ReportingConfig(StrictModel):
    output_dir: Path = DEFAULT_REPORTING_DIR
    base_currency: str | None = None
    extensions: dict[str, ReportExtensionConfig] = Field(default_factory=dict)
    default_formats: tuple[Literal["json", "xlsx", "html"], ...] = (
        "json",
        "xlsx",
        "html",
    )

    @field_validator("extensions")
    @classmethod
    def validate_extension_names(
        cls, value: dict[str, ReportExtensionConfig]
    ) -> dict[str, ReportExtensionConfig]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) for name in value):
            raise ValueError("Report extension names must be lowercase slugs (up to 64 characters)")
        return value

    @field_validator("base_currency")
    @classmethod
    def validate_base_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3:
            raise ValueError("reporting.base_currency must be a three-letter ISO code")
        return normalized


class DocumentsConfig(StrictModel):
    storage_dir: Path = DEFAULT_DOCUMENTS_DIR
    auto_match_threshold: float = Field(default=0.95, ge=0, le=1)
    auto_match_minimum_gap: float = Field(default=0.15, ge=0, le=1)
    extract_pdf_text: bool = True
    local_ocr_enabled: bool = False
    local_ocr_executable: str = "tesseract"
    local_ocr_pdf_renderer: str = "pdftoppm"
    local_ocr_languages: tuple[str, ...] = ("deu", "eng")
    local_ocr_timeout_seconds: int = Field(default=120, ge=1, le=600)


class EmailTargetConfig(StrictModel):
    recipients: tuple[str, ...]
    subject_prefix: str = "Dagoberd"

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("An email target needs at least one recipient")
        for value in values:
            if "@" not in value or "\n" in value or "\r" in value:
                raise ValueError("Invalid email recipient")
        return values

    @field_validator("subject_prefix")
    @classmethod
    def validate_subject_prefix(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("Invalid email subject prefix")
        return value.strip()


class DeliveryConfig(StrictModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = Field(default=587, ge=1, le=65535)
    starttls: bool = True
    use_ssl: bool = False
    from_address: str = ""
    username_secret: str = "DAGOBERD_SMTP_USERNAME"
    password_secret: str = "DAGOBERD_SMTP_PASSWORD"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    targets: dict[str, EmailTargetConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_delivery(self) -> DeliveryConfig:
        if self.enabled and (
            not self.from_address
            or "@" not in self.from_address
            or "\n" in self.from_address
            or "\r" in self.from_address
        ):
            raise ValueError("delivery.from_address is required when delivery is enabled")
        if self.starttls and self.use_ssl:
            raise ValueError("Choose either delivery.starttls or delivery.use_ssl")
        if (
            self.enabled
            and self.host not in {"localhost", "127.0.0.1", "::1"}
            and not (self.starttls or self.use_ssl)
        ):
            raise ValueError("Remote SMTP delivery requires TLS")
        return self


class TransactionSemanticsConfig(StrictModel):
    transfer_max_days: int = Field(default=5, ge=0, le=30)
    transfer_max_fee_minor: int = Field(default=1000, ge=0, le=100000)
    refund_max_days: int = Field(default=90, ge=0, le=730)
    correction_max_days: int = Field(default=14, ge=0, le=90)
    minimum_candidate_score: float = Field(default=0.6, ge=0, le=1)


class WisoTreesConfig(StrictModel):
    mapping_file: Path | None = None


class ImportConfig(StrictModel):
    wiso_trees: WisoTreesConfig = Field(default_factory=WisoTreesConfig)
    mappings: dict[str, dict[str, tuple[str, ...]]] = Field(default_factory=dict)
    profiles: dict[str, ImportProfileConfig] = Field(default_factory=dict)
    max_file_size_mib: int = Field(default=50, ge=1, le=1024)


class AgentsConfig(StrictModel):
    allow_manual_transactions_mcp: bool = False


class ImportAnnotationConfig(StrictModel):
    ignore_values: tuple[str, ...] = ()
    skip_purpose_prefixes: tuple[str, ...] = ()
    category_column: str | None = None
    category_profile: str | None = None
    category_aliases: dict[str, str] = Field(default_factory=dict)
    dimensions: dict[str, str] = Field(default_factory=dict)
    dimension_aliases: dict[str, str] = Field(default_factory=dict)
    dimension_ignore_values: tuple[str, ...] = ()
    profile_columns: dict[str, str] = Field(default_factory=dict)
    profile_category_aliases: dict[str, dict[str, str]] = Field(default_factory=dict)
    preserve_unknown_categories: bool = False
    tag_if_positive: dict[str, str] = Field(default_factory=dict)


class ImportProfileConfig(StrictModel):
    source: Literal["tabular", "camt053", "wiso365"] = "tabular"
    mapping: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    source_attributes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    opening_balance_values: tuple[str, ...] = ("opening_balance", "Eröffnungssaldo")
    account_types: dict[
        str,
        Literal[
            "checking",
            "credit_card",
            "savings",
            "cash",
            "loan",
            "asset",
            "liability",
        ],
    ] = Field(default_factory=dict)
    account_display_names: dict[str, str] = Field(default_factory=dict)
    annotations: ImportAnnotationConfig | None = None
    delimiter: Literal[";", ",", "\t"] | None = None
    encoding: Literal["utf-8-sig", "utf-8", "latin-1", "cp1252"] = "utf-8-sig"
    sheet_name: str | int = 0
    default_currency: str = "EUR"
    date_formats: tuple[str, ...] = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d")
    decimal_separator: Literal[".", ","] | None = None
    header_row: int = Field(default=0, ge=0)

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3:
            raise ValueError("default_currency must be a three-letter ISO code")
        return normalized


class AppConfig(StrictModel):
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    comdirect: ComdirectConfig = Field(default_factory=ComdirectConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    transaction_semantics: TransactionSemanticsConfig = Field(
        default_factory=TransactionSemanticsConfig
    )
    imports: ImportConfig = Field(default_factory=ImportConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: AppConfig
    project_path: Path
    local_path: Path
    registry_path: Path | None = None


DEFAULT_CONFIG_TOML = """# Dagoberd project configuration (no secrets)
# Machine-specific overrides: dagoberd.local.toml (gitignored; copy patterns from below)

[display]
# Calendar-date presentation only; import date_formats remain parsing settings.
# locale = "de-DE" # Otherwise use browser/system locale; fallback: DD.MM.YYYY.
date_format = "locale" # locale, iso, dmy-dot, dmy-slash, mdy-slash, ymd-slash
# date_format = "%d.%m.%Y" # Custom numeric pattern: %d, %m and %Y exactly once.

[database]
path = "output/dagoberd.db"

# Optional private data roots and experimental Comdirect settings belong in dagoberd.local.toml:
#
# [data]
# root = "/path/to/private-workspace"
#
# [cloud]
# workspace = "/absolute/path/to/sync/Dagoberd"
#
# Comdirect API sync (experimental and incomplete). File/Excel imports use import profiles only; leave disabled
# unless you store credentials in the OS keyring (see doc/RUNBOOK.md):
# [comdirect.connections.private]
# enabled = false
# session_mode = "ephemeral"
# keychain_session_max_age_hours = 12
# allow_mcp_sync = false
#
# When enabled = true, set secrets interactively (never in TOML):
#   poetry run dagoberd config set-secret comdirect.private.username
#   poetry run dagoberd config set-secret comdirect.private.client-id
#   poetry run dagoberd config set-secret comdirect.private.client-secret

[comdirect]
# Every Comdirect connection has a stable local name and separate Keychain entries.
# [comdirect.connections.private]
# enabled = true
# session_mode = "ephemeral"
# keychain_session_max_age_hours = 12
# allow_mcp_sync = false

[llm]
enabled = false
mode = "local"
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = ""
api_key_secret = "DAGOBERD_LLM_API_KEY"
allow_cloud = false
timeout_seconds = 30.0
temperature = 0.0
max_tokens = 512
batch_size = 10
max_retries = 2

[privacy]
mask_visible_characters = 4
max_text_length = 500
allow_document_content_for_llm = false

[reporting]
output_dir = "output"
default_formats = ["json", "xlsx", "html"]
base_currency = "EUR"

[documents]
storage_dir = "output/documents"
auto_match_threshold = 0.95
auto_match_minimum_gap = 0.15
extract_pdf_text = true
local_ocr_enabled = false
local_ocr_executable = "tesseract"
local_ocr_pdf_renderer = "pdftoppm"
local_ocr_languages = ["deu", "eng"]
local_ocr_timeout_seconds = 120

[delivery]
enabled = false
host = "localhost"
port = 587
starttls = true
use_ssl = false
from_address = ""
username_secret = "DAGOBERD_SMTP_USERNAME"
password_secret = "DAGOBERD_SMTP_PASSWORD"
timeout_seconds = 30.0

# Keep local recipients in dagoberd.local.toml:
# [delivery.targets.personal]
# recipients = ["name@example.org"]
# subject_prefix = "My finances"
#
# See above: [data].root, [cloud].workspace and Comdirect enabled=false for file-only imports.

[transaction_semantics]
# Increase (for example to 14) if monthly Visa settlements occur farther from the checking debit date.
transfer_max_days = 5
transfer_max_fee_minor = 1000
refund_max_days = 90
correction_max_days = 14
minimum_candidate_score = 0.6

[imports]
max_file_size_mib = 50

[agents]
allow_manual_transactions_mcp = false

# Example reusable tabular mapping profile:
# [imports.profiles.bank_export]
# source = "tabular"
# delimiter = ";"
# encoding = "utf-8-sig"
# default_currency = "EUR"
# date_formats = ["%d/%m/%Y"]
# decimal_separator = ","
# [imports.profiles.bank_export.mapping]
# booking_date = ["Fecha"]
# valuta_date = ["Fecha valor"]
# amount = ["Importe"]
# purpose = ["Concepto"]

# Comdirect Visa export (CSV/XLSX with original German source headers):
# [imports.profiles.visa_comdirect]
# source = "tabular"
# delimiter = ";"
# encoding = "utf-8-sig"
# default_currency = "EUR"
# date_formats = ["%d.%m.%Y", "%Y-%m-%d"]
# decimal_separator = ","
# [imports.profiles.visa_comdirect.mapping]
# booking_date = ["Datum", "Buchungsdatum"]
# amount = ["Betrag", "Umsatz"]
# purpose = ["Verwendungszweck", "Beschreibung"]
# reference = ["Referenz", "Beleg"]
# counterparty = ["Händler", "Beguenstigter"]

# Optional stable identity for an isolated workspace; generated by workspaces init/add.
# [workspace]
# id = "00000000-0000-4000-8000-000000000001"
# name = "Private"
#
# Optional shared cloud root; each UUID gets its own workspaces/<id>/ directory.
# Configure the machine path in dagoberd.local.toml; do not combine with cloud.workspace.
# [cloud]
# root = "/path/to/synchronized-files"
"""


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    use_environment: bool = True,
) -> LoadedConfig:
    explicit_path = path is not None
    project_path = Path(path or PROJECT_CONFIG_NAME).resolve()
    if explicit_path and not project_path.is_file():
        raise FileNotFoundError(f"Configuration does not exist: {project_path}")
    data: dict[str, Any] = {}
    if project_path.is_file():
        data = _read_toml(project_path)
    local_path = project_path.with_name(LOCAL_CONFIG_NAME)
    if local_path.is_file():
        data = _merge(data, _read_toml(local_path))
    if overrides:
        data = _merge(data, overrides)
    base_dir = project_path.parent
    if not use_environment and (
        os.environ.get(DATA_ROOT_ENV) or os.environ.get(CLOUD_WORKSPACE_ENV)
    ):
        raise ValueError("Global data path environment overrides are unavailable for workspaces")
    data_root = _resolve_data_root(base_dir, data)
    cloud_workspace = _resolve_cloud_workspace(base_dir, data)
    cloud_data = data.get("cloud", {})
    if cloud_data.get("root"):
        if cloud_workspace is not None:
            raise ValueError("Use either cloud.root or cloud.workspace")
        identity = data.get("workspace", {}).get("id")
        if not identity:
            raise ValueError("cloud.root requires a stable workspace.id")
        cloud_workspace = (
            _resolve_configured_root(base_dir, cloud_data["root"])
            / "workspaces"
            / str(UUID(str(identity)))
        )
    _apply_root_derived_paths(data, data_root, cloud_workspace)
    config = AppConfig.model_validate(data)
    config = config.model_copy(
        update={
            "imports": config.imports.model_copy(
                update={
                    "wiso_trees": config.imports.wiso_trees.model_copy(
                        update={
                            "mapping_file": _resolve_local_path(
                                base_dir, config.imports.wiso_trees.mapping_file
                            )
                            if config.imports.wiso_trees.mapping_file is not None
                            else None,
                        }
                    ),
                }
            ),
            "data": config.data.model_copy(update={"root": data_root}),
            "cloud": config.cloud.model_copy(
                update={
                    "workspace": cloud_workspace,
                    "root": _resolve_configured_root(base_dir, config.cloud.root)
                    if config.cloud.root is not None
                    else None,
                }
            ),
            "database": config.database.model_copy(
                update={
                    "path": _resolve_local_path(
                        base_dir,
                        config.database.path,
                    )
                }
            ),
            "reporting": config.reporting.model_copy(
                update={
                    "output_dir": _resolve_local_path(
                        base_dir,
                        config.reporting.output_dir,
                    ),
                    "extensions": {
                        name: extension.model_copy(
                            update={"path": _resolve_local_path(base_dir, extension.path)}
                        )
                        for name, extension in config.reporting.extensions.items()
                    },
                }
            ),
            "documents": config.documents.model_copy(
                update={
                    "storage_dir": _resolve_local_path(
                        base_dir,
                        config.documents.storage_dir,
                    )
                }
            ),
        }
    )
    return LoadedConfig(config, project_path, local_path)


def _resolve_local_path(base_dir: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base_dir / value).resolve()


def _path_still_default(raw: dict[str, Any], section: str, key: str, default: str) -> bool:
    section_data = raw.get(section, {})
    if not isinstance(section_data, dict):
        return True
    configured = section_data.get(key, default)
    return Path(str(configured)).as_posix() == Path(default).as_posix()


def _apply_root_derived_paths(
    raw: dict[str, Any],
    data_root: Path | None,
    cloud_workspace: Path | None,
) -> None:
    if data_root and _path_still_default(
        raw,
        "database",
        "path",
        str(DEFAULT_DATABASE_PATH),
    ):
        database = raw.setdefault("database", {})
        if isinstance(database, dict):
            database["path"] = str(data_root / "ledger" / "dagoberd.db")
    if cloud_workspace:
        if _path_still_default(raw, "documents", "storage_dir", str(DEFAULT_DOCUMENTS_DIR)):
            documents = raw.setdefault("documents", {})
            if isinstance(documents, dict):
                documents["storage_dir"] = str(cloud_workspace / "documents")
        if _path_still_default(raw, "reporting", "output_dir", str(DEFAULT_REPORTING_DIR)):
            reporting = raw.setdefault("reporting", {})
            if isinstance(reporting, dict):
                reporting["output_dir"] = str(cloud_workspace / "reports")


def _resolve_configured_root(base_dir: Path, value: str | Path) -> Path:
    root = Path(value).expanduser()
    return root if root.is_absolute() else (base_dir / root).resolve()


def _resolve_data_root(base_dir: Path, raw: dict[str, Any]) -> Path | None:
    if env := os.environ.get(DATA_ROOT_ENV):
        return Path(env).expanduser().resolve()
    data_section = raw.get("data", {})
    if isinstance(data_section, dict) and data_section.get("root"):
        return _resolve_configured_root(base_dir, data_section["root"])
    return None


def _resolve_cloud_workspace(base_dir: Path, raw: dict[str, Any]) -> Path | None:
    if env := os.environ.get(CLOUD_WORKSPACE_ENV):
        return Path(env).expanduser().resolve()
    cloud_section = raw.get("cloud", {})
    if isinstance(cloud_section, dict) and cloud_section.get("workspace"):
        return _resolve_configured_root(base_dir, cloud_section["workspace"])
    return None


def initialize_project_config(
    path: str | Path | None = None,
    *,
    force: bool = False,
) -> Path:
    target = Path(path or PROJECT_CONFIG_NAME).resolve()
    if target.exists() and not force:
        raise FileExistsError(f"Configuration already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target


def masked_config(config: AppConfig) -> dict[str, Any]:
    """Return configuration without resolving or reading any secret value."""
    value = config.model_dump(mode="json")
    for target in value.get("delivery", {}).get("targets", {}).values():
        target["recipients"] = ["***@***" for _ in target.get("recipients", [])]
    return value
