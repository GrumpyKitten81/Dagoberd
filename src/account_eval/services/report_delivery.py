"""Optional SMTP report delivery with preview, confirmation and audit trail."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import smtplib
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

from account_eval.config import DeliveryConfig
from account_eval.ledger.repository import LedgerRepository
from account_eval.secrets import SecretStore

SMTPFactory = Callable[..., Any]


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    visible = local[:1]
    return f"{visible}***@{domain}" if domain else "***"


class ReportDeliveryService:
    def __init__(
        self,
        repository: LedgerRepository,
        config: DeliveryConfig,
        secrets: SecretStore,
        *,
        smtp_factory: SMTPFactory | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.secrets = secrets
        self.smtp_factory = smtp_factory

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def list_report_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM ledger_report_runs
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "report_run_id": row["id"],
                "profile": row["profile_code"],
                "paths": json.loads(row["output_paths_json"]),
                "transaction_count": row["transaction_count"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def preview(
        self,
        target_name: str,
        report_run_id: str = "latest",
        *,
        ttl_minutes: int = 30,
    ) -> dict[str, object]:
        if not self.config.enabled:
            raise ValueError("Report delivery is disabled in the local configuration")
        target = self.config.targets.get(target_name)
        if target is None:
            raise KeyError(f"Unknown delivery target {target_name}")
        with self.repository.database.connect() as connection:
            if report_run_id == "latest":
                report = connection.execute(
                    "SELECT * FROM ledger_report_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            else:
                report = connection.execute(
                    "SELECT * FROM ledger_report_runs WHERE id=?", (report_run_id,)
                ).fetchone()
        if report is None:
            raise KeyError("No matching report run exists")
        artifacts: list[dict[str, object]] = []
        for raw_path in json.loads(report["output_paths_json"]):
            path = Path(raw_path).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Report artifact no longer exists: {path}")
            artifacts.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": self._digest(path),
                }
            )
        message = {
            "from": self.config.from_address,
            "subject": f"{target.subject_prefix}: Report {report['id']}",
            "transport": {
                "host": self.config.host,
                "port": self.config.port,
                "starttls": self.config.starttls,
                "use_ssl": self.config.use_ssl,
            },
        }
        canonical = json.dumps(
            {
                "report_run_id": report["id"],
                "target": target_name,
                "recipients": target.recipients,
                "message": message,
                "artifacts": artifacts,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        identifier = f"delivery-preview-{uuid4()}"
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_delivery_previews(
                       id, report_run_id, target_name, recipients_json,
                       message_json, artifacts_json, fingerprint, status, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)""",
                (
                    identifier,
                    report["id"],
                    target_name,
                    json.dumps(target.recipients),
                    json.dumps(message, sort_keys=True),
                    json.dumps(artifacts, sort_keys=True),
                    fingerprint,
                    expires_at.isoformat(),
                ),
            )
            self.repository._audit(
                connection,
                "report_delivery",
                identifier,
                "preview",
                None,
                {
                    "report_run_id": report["id"],
                    "target": target_name,
                    "recipients": [_mask_email(item) for item in target.recipients],
                    "artifact_count": len(artifacts),
                    "fingerprint": fingerprint,
                },
            )
        return self.get(identifier)

    def get(self, preview_id: str) -> dict[str, object]:
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_delivery_previews WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown report delivery preview {preview_id}")
        recipients: list[str] = json.loads(row["recipients_json"])
        return {
            "preview_id": row["id"],
            "report_run_id": row["report_run_id"],
            "target": row["target_name"],
            "recipients": [_mask_email(item) for item in recipients],
            "message": {
                **json.loads(row["message_json"]),
                "from": _mask_email(json.loads(row["message_json"])["from"]),
            },
            "artifacts": json.loads(row["artifacts_json"]),
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "requires_local_confirmation": True,
        }

    def send(
        self,
        preview_id: str,
        fingerprint: str,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self.repository.database.connect() as connection:
            previous = connection.execute(
                "SELECT status, result_json FROM ledger_service_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                previous_result = json.loads(previous["result_json"])
                if previous["status"] == "completed":
                    return previous_result
                raise ValueError(
                    "This delivery idempotency key has a non-completed prior run; "
                    "inspect the audit before choosing a new key"
                )
            row = connection.execute(
                "SELECT * FROM ledger_delivery_previews WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown report delivery preview {preview_id}")
        if row["status"] != "ready":
            raise ValueError(f"Report delivery preview is not sendable: {row['status']}")
        if row["fingerprint"] != fingerprint:
            raise ValueError("Report delivery fingerprint does not match")
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            with self.repository.database.connect() as connection:
                connection.execute(
                    "UPDATE ledger_delivery_previews SET status='expired' WHERE id=?",
                    (preview_id,),
                )
            raise ValueError("Report delivery preview has expired")
        artifacts: list[dict[str, object]] = json.loads(row["artifacts_json"])
        paths: list[Path] = []
        for artifact in artifacts:
            path = Path(str(artifact["path"]))
            if not path.is_file() or self._digest(path) != artifact["sha256"]:
                with self.repository.database.connect() as connection:
                    connection.execute(
                        "UPDATE ledger_delivery_previews SET status='invalidated' WHERE id=?",
                        (preview_id,),
                    )
                raise ValueError("A report artifact changed after the delivery preview")
            paths.append(path)
        recipients: list[str] = json.loads(row["recipients_json"])
        message_config: dict[str, object] = json.loads(row["message_json"])
        current_transport = {
            "host": self.config.host,
            "port": self.config.port,
            "starttls": self.config.starttls,
            "use_ssl": self.config.use_ssl,
        }
        if message_config["transport"] != current_transport:
            raise ValueError("SMTP transport configuration changed after the preview")
        message = EmailMessage()
        message["From"] = str(message_config["from"])
        message["To"] = ", ".join(recipients)
        message["Subject"] = str(message_config["subject"])
        message.set_content(
            "AccountEval report attached. Local analysis; not tax or investment advice."
        )
        for path in paths:
            content_type, _ = mimetypes.guess_type(path.name)
            main_type, sub_type = (content_type or "application/octet-stream").split("/", 1)
            message.add_attachment(
                path.read_bytes(),
                maintype=main_type,
                subtype=sub_type,
                filename=path.name,
            )
        run_id = f"service-run-{uuid4()}"
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_service_runs(
                       id, service, operation, status, idempotency_key, result_json
                   ) VALUES (?, 'smtp', 'report_delivery', 'running', ?, '{}')""",
                (run_id, idempotency_key),
            )
        username = self.secrets.get(self.config.username_secret)
        password = self.secrets.get(self.config.password_secret)
        factory = self.smtp_factory or (smtplib.SMTP_SSL if self.config.use_ssl else smtplib.SMTP)
        try:
            smtp = factory(self.config.host, self.config.port, timeout=self.config.timeout_seconds)
            try:
                if self.config.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password or "")
                smtp.send_message(message)
            finally:
                smtp.quit()
        except Exception as error:
            failed = {
                "preview_id": preview_id,
                "report_run_id": row["report_run_id"],
                "target": row["target_name"],
                "status": "failed",
                "error": type(error).__name__,
            }
            with self.repository.database.connect() as connection:
                connection.execute(
                    """UPDATE ledger_service_runs
                       SET status='failed', result_json=?, completed_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (json.dumps(failed, sort_keys=True), run_id),
                )
                self.repository._audit(
                    connection,
                    "report_delivery",
                    preview_id,
                    "failed",
                    None,
                    failed,
                    idempotency_key=f"smtp:{idempotency_key}",
                )
            raise RuntimeError("SMTP report delivery failed") from error
        result: dict[str, object] = {
            "preview_id": preview_id,
            "report_run_id": row["report_run_id"],
            "target": row["target_name"],
            "recipients": [_mask_email(item) for item in recipients],
            "artifact_count": len(paths),
            "status": "sent",
        }
        with self.repository.database.connect() as connection:
            connection.execute(
                """UPDATE ledger_service_runs
                   SET status='completed', result_json=?, completed_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (json.dumps(result, sort_keys=True), run_id),
            )
            connection.execute(
                """UPDATE ledger_delivery_previews
                   SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?""",
                (preview_id,),
            )
            self.repository._audit(
                connection,
                "report_delivery",
                preview_id,
                "sent",
                None,
                result,
                idempotency_key=f"smtp:{idempotency_key}",
            )
        return result
