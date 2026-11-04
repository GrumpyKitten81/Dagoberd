"""Safe local onboarding, demo data, agent runtime and offline self-test."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import MutableMapping
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import tomlkit
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from account_eval.config import DEFAULT_CONFIG_TOML, initialize_project_config, load_config
from account_eval.documents.matching import DocumentMatcher
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.profiles import install_builtin_profiles
from account_eval.reporting.ledger_reports import LedgerReportGenerator
from account_eval.services.portability import PortabilityService

LLM_PRESETS: dict[str, tuple[str, str, bool]] = {
    "disabled": ("http://127.0.0.1:11434/v1", "local", False),
    "ollama": ("http://127.0.0.1:11434/v1", "local", False),
    "lm-studio": ("http://127.0.0.1:1234/v1", "local", False),
    "cloud": ("https://api.openai.com/v1", "cloud", True),
    "custom-local": ("http://127.0.0.1:11434/v1", "local", False),
}


@dataclass(frozen=True, slots=True)
class SetupAnswers:
    comdirect_enabled: bool = False
    llm_preset: str = "disabled"
    llm_model: str = ""
    llm_base_url: str | None = None
    base_currency: str = "EUR"

    def validated(self) -> SetupAnswers:
        if self.llm_preset not in LLM_PRESETS:
            raise ValueError(f"Unknown LLM preset: {self.llm_preset}")
        currency = self.base_currency.strip().upper()
        if len(currency) != 3:
            raise ValueError("Base currency must be a three-letter ISO code")
        if self.llm_preset != "disabled" and not self.llm_model.strip():
            raise ValueError("An LLM model is required for an enabled preset")
        return SetupAnswers(
            comdirect_enabled=self.comdirect_enabled,
            llm_preset=self.llm_preset,
            llm_model=self.llm_model.strip(),
            llm_base_url=self.llm_base_url.strip() if self.llm_base_url else None,
            base_currency=currency,
        )


def configure_local_project(project_config: str | Path, answers: SetupAnswers) -> dict[str, Any]:
    """Atomically write only non-secret local overrides."""
    checked = answers.validated()
    project_path = Path(project_config).resolve()
    if not project_path.exists():
        initialize_project_config(project_path)
    local_path = project_path.with_name("account-eval.local.toml")
    default_url, mode, allow_cloud = LLM_PRESETS[checked.llm_preset]
    base_url = checked.llm_base_url or default_url
    enabled = checked.llm_preset != "disabled"
    document = (
        tomlkit.parse(local_path.read_text(encoding="utf-8"))
        if local_path.is_file()
        else tomlkit.document()
    )
    _update_setup_values(
        document,
        {
            "comdirect": {"connections": {"primary": {"enabled": checked.comdirect_enabled}}},
            "llm": {
                "enabled": enabled,
                "mode": mode,
                "provider": "openai-compatible",
                "base_url": base_url,
                "model": checked.llm_model,
                "allow_cloud": allow_cloud,
            },
            "reporting": {"base_currency": checked.base_currency},
        },
    )
    content = tomlkit.dumps(document)
    loaded = load_config(project_path, overrides=tomllib.loads(content))
    _secure_atomic_write(local_path, content)
    LedgerRepository(LedgerDatabase(loaded.config.database.path))
    return {
        "configuration": str(project_path),
        "local_override": str(local_path),
        "comdirect_enabled": checked.comdirect_enabled,
        "llm": {
            "enabled": enabled,
            "preset": checked.llm_preset,
            "mode": mode,
            "base_url": base_url,
            "model": checked.llm_model,
            "allow_cloud": allow_cloud,
        },
        "base_currency": checked.base_currency,
        "secrets_written": False,
    }


def _update_setup_values(settings: MutableMapping[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict):
            section = settings.setdefault(key, tomlkit.table())
            if not isinstance(section, MutableMapping):
                raise ValueError(f"Setup configuration section {key} must be a table")
            _update_setup_values(section, value)
        else:
            settings[key] = value


def configure_agent_launcher(workspace: str | Path = ".") -> dict[str, Any]:
    """Create an ignored launcher using the exact active Python environment."""
    root = Path(workspace).resolve()
    runtime_dir = root / ".account-eval"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    python_path = sys.executable
    if os.name == "nt":
        launcher = runtime_dir / "mcp.cmd"
        content = f'@"{python_path}" -m account_eval.mcp_server %*\r\n'
    else:
        launcher = runtime_dir / "mcp"
        content = f'#!/bin/sh\nexec "{python_path}" -m account_eval.mcp_server "$@"\n'
    _secure_atomic_write(launcher, content, executable=True)
    return {
        "launcher": str(launcher),
        "python": sys.executable,
        "codex_command": "./Scripts/account-eval-mcp.sh",
        "opencode_command": ["./Scripts/account-eval-mcp.sh"],
        "required": False,
    }


async def _mcp_handshake(command: str, config: Path, database: Path) -> dict[str, Any]:
    env = {
        **os.environ,
        "ACCOUNT_EVAL_CONFIG": str(config),
        "ACCOUNT_EVAL_DB": str(database),
    }
    env.pop("ACCOUNT_EVAL_WORKSPACE", None)
    if load_config(config).config.workspace.id is not None:
        from account_eval.workspaces import CURRENT_WORKSPACE

        env.pop("ACCOUNT_EVAL_DB", None)
        current = CURRENT_WORKSPACE.get()
        if current and current.registry_path:
            env["ACCOUNT_EVAL_WORKSPACE_REGISTRY"] = str(current.registry_path)
    if os.name == "nt" and str(command).lower().endswith(".cmd"):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "account_eval.mcp_server"],
            env=env,
        )
    else:
        parameters = StdioServerParameters(command=command, env=env)
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        tools = await session.list_tools()
    return {
        "server": initialized.serverInfo.name,
        "version": initialized.serverInfo.version,
        "tool_count": len(tools.tools),
        "safe_tools_present": all(
            name in {tool.name for tool in tools.tools}
            for name in ("system_status", "list_accounts", "list_transactions")
        ),
    }


_OPENCODE_AGENT_FILES = (
    "account-eval-explorer.md",
    "account-eval-reviewer.md",
)
_OPENCODE_COMMAND_FILES = (
    "doctor.md",
    "agents-configure.md",
    "handoff.md",
    "finance.md",
)
_CODEX_AGENT_REQUIREMENTS: dict[str, dict[str, str]] = {
    "account-eval-explorer.toml": {
        "name": "account_eval_explorer",
        "sandbox_mode": "read-only",
    },
    "account-eval-executor.toml": {
        "name": "account_eval_executor",
        "sandbox_mode": "workspace-write",
    },
    "account-eval-reviewer.toml": {
        "name": "account_eval_reviewer",
        "sandbox_mode": "read-only",
    },
}


def diagnose_opencode_integration(workspace: str | Path = ".") -> dict[str, Any]:
    """Check tracked OpenCode project files and MCP wiring."""
    root = Path(workspace).resolve()
    issues: list[str] = []
    opencode_json = root / "opencode.json"
    if not opencode_json.exists():
        issues.append("opencode_json_missing")
    else:
        try:
            payload = json.loads(opencode_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            issues.append("opencode_json_invalid")
        else:
            mcp = payload.get("mcp", {}).get("account-eval")
            if not isinstance(mcp, dict):
                issues.append("opencode_mcp_missing")
            elif mcp.get("command") != ["./Scripts/account-eval-mcp.sh"]:
                issues.append("opencode_mcp_command_mismatch")
            skills = payload.get("skills")
            if not isinstance(skills, dict):
                issues.append("opencode_skills_invalid")
            elif skills.get("paths") != [".agents/skills"]:
                issues.append("opencode_skills_path_unexpected")

    if not (root / ".opencode" / "AGENTS.md").exists():
        issues.append("opencode_agents_md_missing")
    for filename in _OPENCODE_AGENT_FILES:
        if not (root / ".opencode" / "agents" / filename).exists():
            issues.append(f"opencode_agent_missing:{filename}")
    for filename in _OPENCODE_COMMAND_FILES:
        if not (root / ".opencode" / "commands" / filename).exists():
            issues.append(f"opencode_command_missing:{filename}")

    return {
        "status": "ok" if not issues else "warning",
        "issues": issues,
        "config": str(opencode_json) if opencode_json.exists() else None,
    }


def diagnose_claude_integration(workspace: str | Path = ".") -> dict[str, Any]:
    """Check tracked Claude Code project supplement."""
    root = Path(workspace).resolve()
    issues: list[str] = []
    if not (root / ".claude" / "AGENTS.md").exists():
        issues.append("claude_agents_md_missing")
    return {
        "status": "ok" if not issues else "warning",
        "issues": issues,
    }


def diagnose_codex_integration(workspace: str | Path = ".") -> dict[str, Any]:
    """Check tracked Codex agent definitions without reading user-level settings."""
    root = Path(workspace).resolve()
    issues: list[str] = []
    config_path = root / ".codex" / "config.toml"
    if not config_path.exists():
        issues.append("codex_config_missing")
    else:
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            issues.append("codex_config_invalid")
        else:
            mcp = config.get("mcp_servers", {}).get("account-eval")
            if not isinstance(mcp, dict):
                issues.append("codex_mcp_missing")
            elif mcp.get("command") != "./Scripts/account-eval-mcp.sh":
                issues.append("codex_mcp_command_mismatch")

    if not (root / ".codex" / "AGENTS.md").exists():
        issues.append("codex_agents_md_missing")
    agents_dir = root / ".codex" / "agents"
    for filename, expected in _CODEX_AGENT_REQUIREMENTS.items():
        agent_path = agents_dir / filename
        if not agent_path.exists():
            issues.append(f"codex_agent_missing:{filename}")
            continue
        try:
            definition = tomllib.loads(agent_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            issues.append(f"codex_agent_invalid:{filename}")
            continue
        for field, value in expected.items():
            if definition.get(field) != value:
                issues.append(f"codex_agent_{field}_mismatch:{filename}")
        if not isinstance(definition.get("description"), str):
            issues.append(f"codex_agent_description_missing:{filename}")
        if not isinstance(definition.get("developer_instructions"), str):
            issues.append(f"codex_agent_instructions_missing:{filename}")

    return {
        "status": "ok" if not issues else "warning",
        "issues": issues,
        "config": str(config_path) if config_path.exists() else None,
    }


def diagnose_agent_runtime(
    workspace: str | Path = ".",
    *,
    config_path: str | Path = "account-eval.toml",
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    opencode = diagnose_opencode_integration(root)
    codex = diagnose_codex_integration(root)
    claude = diagnose_claude_integration(root)
    launcher = root / ".account-eval" / ("mcp.cmd" if os.name == "nt" else "mcp")
    if not launcher.exists():
        return {
            "status": "error",
            "error": "launcher_missing",
            "next_action": "run account-eval agents configure",
            "opencode": opencode,
            "codex": codex,
            "claude": claude,
        }
    loaded = load_config(config_path)
    try:
        handshake = asyncio.run(
            _mcp_handshake(str(launcher), loaded.project_path, loaded.config.database.path)
        )
    except Exception as error:
        return {
            "status": "error",
            "error": "mcp_start_failed",
            "message": type(error).__name__,
            "opencode": opencode,
            "codex": codex,
            "claude": claude,
        }
    status = (
        "ok"
        if opencode["status"] == "ok" and codex["status"] == "ok" and claude["status"] == "ok"
        else "warning"
    )
    return {
        "status": status,
        "launcher": str(launcher),
        "handshake": handshake,
        "opencode": opencode,
        "codex": codex,
        "claude": claude,
    }


def create_demo_workspace(destination: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Create a self-contained synthetic workspace through public domain services."""
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"Demo workspace is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "account-eval.toml"
    if config_path.exists():
        config_path.unlink()
    initialize_project_config(config_path)
    loaded = load_config(config_path)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    transactions = (
        NormalizedTransaction(
            "demo-salary",
            date(2026, 7, 1),
            Decimal("3200.00"),
            "EUR",
            "July salary",
            "demo-checking",
            counterparty="Demo employer",
            reference="DEMO-SALARY",
        ),
        NormalizedTransaction(
            "demo-rent",
            date(2026, 7, 2),
            Decimal("-950.00"),
            "EUR",
            "July housing costs",
            "demo-checking",
            counterparty="Demo landlord",
            reference="DEMO-RENT",
        ),
        NormalizedTransaction(
            "demo-card",
            date(2026, 7, 3),
            Decimal("-49.95"),
            "EUR",
            "Software subscription",
            "demo-card",
            counterparty="Demo Software Ltd",
            reference="DEMO-CARD",
        ),
        NormalizedTransaction(
            "demo-pending",
            date(2026, 7, 4),
            Decimal("-12.50"),
            "EUR",
            "Pending purchase",
            "demo-card",
            booking_status="pending",
        ),
        NormalizedTransaction(
            "demo-cash",
            date(2026, 7, 5),
            Decimal("-20.00"),
            "EUR",
            "Cash expense",
            "demo-cash",
        ),
    )
    result = repository.import_transactions("demo", "Demo Provider", transactions)
    document_dir = root / "fixtures"
    document_dir.mkdir(exist_ok=True)
    document = document_dir / "demo-receipt.txt"
    document.write_text("Synthetic AccountEval demo receipt", encoding="utf-8")
    matcher = DocumentMatcher(repository, loaded.config.documents.storage_dir)
    matcher.import_file(
        document,
        amount=Decimal("49.95"),
        document_date=date(2026, 7, 3),
        merchant="Demo Software Ltd",
        reference="DEMO-INVOICE-1",
    )
    report_paths = LedgerReportGenerator(repository, loaded.config.reporting.output_dir).generate(
        formats=("json", "xlsx", "html")
    )
    return {
        "workspace": str(root),
        "configuration": str(config_path),
        "database": str(loaded.config.database.path),
        "import": asdict(result),
        "accounts": len(repository.list_accounts()),
        "transactions": len(repository.list_transactions(limit=None)),
        "documents": 1,
        "reports": [str(path) for path in report_paths],
        "contains_personal_data": False,
    }


def run_offline_self_test() -> dict[str, Any]:
    """Exercise release-critical local behavior without external services."""
    temporary = Path(tempfile.mkdtemp(prefix="account-eval-self-test-"))
    result = _run_offline_self_test_in(temporary)
    gc.collect()
    _remove_tree_with_retry(temporary)
    return result


def _run_offline_self_test_in(root: Path) -> dict[str, Any]:
    demo = create_demo_workspace(root / "demo")
    config_path = Path(demo["configuration"])
    loaded = load_config(config_path)
    launcher = configure_agent_launcher(root / "demo")
    handshake = asyncio.run(
        _mcp_handshake(launcher["launcher"], config_path, loaded.config.database.path)
    )
    portability = PortabilityService(
        loaded.config.database.path,
        loaded.config.documents.storage_dir,
        loaded.config.reporting.output_dir,
        config_path,
    )
    export = portability.export(root / "demo-export.aex")
    verify = portability.verify(root / "demo-export.aex")
    return {
        "status": "ok",
        "demo": demo,
        "mcp": handshake,
        "portability": {"export": export, "verify": verify},
        "external_network_used": False,
    }


def _remove_tree_with_retry(path: Path, *, attempts: int = 30) -> None:
    last_error: OSError | None = None
    for attempt in range(attempts):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.2 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _secure_atomic_write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    mode = stat.S_IRUSR | stat.S_IWUSR
    if executable:
        mode |= stat.S_IXUSR
    os.chmod(temporary, mode)
    temporary.replace(path)


def copy_default_configuration(destination: str | Path) -> Path:
    """Write the public default template with restrictive local permissions."""
    target = Path(destination).resolve()
    _secure_atomic_write(target, DEFAULT_CONFIG_TOML)
    return target


def remove_demo_workspace(destination: str | Path) -> None:
    """Remove only an explicitly named demo directory."""
    target = Path(destination).resolve()
    if target.name != "demo" or not (target / "account-eval.toml").is_file():
        raise ValueError("Refusing to remove a directory that is not an AccountEval demo")
    shutil.rmtree(target)
