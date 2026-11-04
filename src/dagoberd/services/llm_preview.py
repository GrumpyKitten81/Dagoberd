"""Explicit, inspectable gate for local and cloud LLM classification calls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dagoberd.accounts import AccountScope
from dagoberd.config import LLMConfig, PrivacyConfig
from dagoberd.contracts import LLM_PREVIEW_CONTRACT_VERSION
from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError
from dagoberd.llm import LLMClassificationInput, LLMProvider
from dagoberd.llm.openai_compatible import PROMPT_VERSION


class LLMPreviewService:
    """Persist the exact redacted LLM payload before any provider request."""

    def __init__(
        self,
        repository: LedgerRepository,
        provider: LLMProvider | None,
        config: LLMConfig,
        privacy: PrivacyConfig,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.config = config
        self.classification = ClassificationService(repository, provider, privacy, config.model)

    def create(
        self,
        profile_code: str,
        *,
        transaction_ids: list[str] | None = None,
        account_scope: AccountScope | None = None,
        limit: int = 100,
        ttl_minutes: int = 30,
    ) -> dict[str, object]:
        if not self.config.enabled:
            raise ValueError("LLM is disabled; configure it with 'dagoberd setup'")
        request_context = self._request_context()
        if transaction_ids is None:
            transaction_ids = [
                row["id"]
                for row in self.repository.list_transactions(
                    limit=None,
                    profile_code=profile_code,
                    account_scope=account_scope,
                )
                if not row.get("category_code")
            ][:limit]
        payloads: list[dict[str, object]] = []
        versions: dict[str, int] = {}
        with self.repository.database.connect() as connection:
            for transaction_id in transaction_ids[:limit]:
                if not self.classification.stage_enabled(transaction_id, "llm"):
                    continue
                if self.classification.classify_deterministic(transaction_id, profile_code):
                    continue
                item = self.classification.build_llm_input(transaction_id, profile_code)
                if item is None:
                    continue
                row = connection.execute(
                    "SELECT version FROM ledger_transactions WHERE id=?",
                    (transaction_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown transaction {transaction_id}")
                payloads.append(item.model_dump(mode="json"))
                versions[transaction_id] = int(row["version"])
        payload_json = json.dumps(
            payloads,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical = json.dumps(
            {
                "request_context": request_context,
                "profile": profile_code,
                "transaction_versions": versions,
                "payloads": payloads,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        identifier = f"llm-preview-{uuid4()}"
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_llm_previews(
                       id, profile_code, mode, provider, model, payload_json,
                       transaction_versions_json, fingerprint, status, expires_at,
                       request_context_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
                (
                    identifier,
                    profile_code,
                    self.config.mode,
                    self.config.provider,
                    self.config.model,
                    payload_json,
                    json.dumps(versions, sort_keys=True),
                    fingerprint,
                    expires_at.isoformat(),
                    json.dumps(request_context, sort_keys=True),
                ),
            )
            self.repository._audit(
                connection,
                "llm_preview",
                identifier,
                "created",
                None,
                {
                    "mode": self.config.mode,
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "items": len(payloads),
                    "fingerprint": fingerprint,
                },
            )
        return self.get(identifier)

    def _request_context(self) -> dict[str, object]:
        config = LLMConfig.model_validate(self.config.model_dump())
        return {
            "contract_version": LLM_PREVIEW_CONTRACT_VERSION,
            "llm": config.model_dump(mode="json"),
            "privacy": self.classification.privacy.model_dump(mode="json"),
            "prompt_version": PROMPT_VERSION,
        }

    def get(self, preview_id: str) -> dict[str, object]:
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_llm_previews WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown LLM preview {preview_id}")
        return {
            "contract_version": LLM_PREVIEW_CONTRACT_VERSION,
            "preview_id": row["id"],
            "profile": row["profile_code"],
            "mode": row["mode"],
            "provider": row["provider"],
            "model": row["model"],
            "request_context": json.loads(row["request_context_json"])
            if row["request_context_json"]
            else None,
            "payloads": json.loads(row["payload_json"]),
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "results": json.loads(row["result_json"]) if row["result_json"] else None,
            "requires_confirmation": True,
            "notice": (
                "In cloud mode, only the displayed redacted "
                "payloads are sent to the configured provider."
            ),
        }

    def execute(self, preview_id: str, fingerprint: str) -> dict[str, object]:
        if self.provider is None:
            raise ValueError("No active LLM provider is configured")
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_llm_previews WHERE id=?", (preview_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown LLM preview {preview_id}")
            if row["fingerprint"] != fingerprint:
                raise ValueError("LLM preview fingerprint does not match")
            if row["status"] == "executed":
                return self.get(preview_id)
            if row["status"] != "ready":
                raise ValueError(f"LLM preview is not executable: {row['status']}")
            if row["request_context_json"] is None or (
                json.loads(row["request_context_json"]) != self._request_context()
            ):
                connection.execute(
                    "UPDATE ledger_llm_previews SET status='invalidated' WHERE id=?", (preview_id,)
                )
                connection.commit()
                raise ValueError("LLM configuration changed after the preview")
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
                connection.execute(
                    "UPDATE ledger_llm_previews SET status='expired' WHERE id=?",
                    (preview_id,),
                )
                connection.commit()
                raise ValueError("LLM preview has expired; create a new preview")
            versions: dict[str, int] = json.loads(row["transaction_versions_json"])
            for transaction_id, expected in versions.items():
                current = connection.execute(
                    "SELECT version FROM ledger_transactions WHERE id=?", (transaction_id,)
                ).fetchone()
                if current is None or int(current["version"]) != int(expected):
                    connection.execute(
                        "UPDATE ledger_llm_previews SET status='invalidated' WHERE id=?",
                        (preview_id,),
                    )
                    connection.commit()
                    raise VersionConflictError(
                        f"Transaction {transaction_id} changed after the LLM preview"
                    )
            payloads: list[dict[str, object]] = json.loads(row["payload_json"])
            profile = str(row["profile_code"])
            claimed = connection.execute(
                "UPDATE ledger_llm_previews SET status='running' WHERE id=? AND status='ready'",
                (preview_id,),
            )
            if claimed.rowcount != 1:
                raise ValueError("LLM preview was claimed by another execution")
        results: list[dict[str, object]] = []
        try:
            for payload in payloads:
                item = LLMClassificationInput.model_validate(payload)
                suggestion = self.provider.classify(item)
                classification_id = None
                if suggestion is not None:
                    classification_id = self.repository.classify(
                        item.transaction_id,
                        profile,
                        suggestion.category,
                        "llm",
                        suggestion.confidence,
                        suggestion.rationale,
                        "suggested",
                        versions[item.transaction_id],
                        model_version=f"{suggestion.provider}/{suggestion.model}",
                        prompt_hash=suggestion.prompt_hash,
                        metadata={
                            "prompt_version": suggestion.prompt_version,
                            "taxonomy_version": suggestion.taxonomy_version,
                            "llm_preview_id": preview_id,
                            "redaction_mode": "standard-no-document-content",
                            "duration_ms": suggestion.duration_ms,
                        },
                    )
                results.append(
                    {
                        "transaction_id": item.transaction_id,
                        "classification_id": classification_id,
                    }
                )
        except Exception as error:
            with self.repository.database.connect() as connection:
                connection.execute(
                    """UPDATE ledger_llm_previews
                       SET status='failed', result_json=? WHERE id=? AND status='running'""",
                    (json.dumps(results, sort_keys=True), preview_id),
                )
                self.repository._audit(
                    connection,
                    "llm_preview",
                    preview_id,
                    "failed",
                    None,
                    {"completed_items": len(results), "error": type(error).__name__},
                )
            raise
        with self.repository.database.connect() as connection:
            connection.execute(
                """UPDATE ledger_llm_previews
                   SET status='executed', result_json=?, executed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='running'""",
                (json.dumps(results, sort_keys=True), preview_id),
            )
            self.repository._audit(
                connection,
                "llm_preview",
                preview_id,
                "executed",
                None,
                {"items": len(results), "fingerprint": fingerprint},
            )
        return self.get(preview_id)
