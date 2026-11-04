"""Read-only Comdirect adapter with explicit local 2FA and resumable paging.

The authentication and paging flow is derived from mad4ms/comdirect-api-wrapper
0.3.1 under the MIT license; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

import requests

from account_eval.config import ComdirectConnectionConfig
from account_eval.ledger.models import AccountType, NormalizedTransaction, TransactionStatus
from account_eval.secrets import SecretStore

from .base import AccountProvider


class ComdirectError(RuntimeError):
    code = "comdirect_error"


class ComdirectAuthenticationError(ComdirectError):
    code = "authentication_required"


class ComdirectRateLimitError(ComdirectError):
    code = "rate_limited"


class ComdirectTemporaryError(ComdirectError):
    code = "temporary_error"


class ComdirectAPIError(ComdirectError):
    code = "api_error"


@dataclass(frozen=True, slots=True)
class ComdirectSessionState:
    access_token: str
    session_id: str
    refresh_token: str | None
    expires_at: str | None
    authenticated_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> ComdirectSessionState:
        payload = json.loads(value)
        return cls(
            access_token=payload["access_token"],
            session_id=payload["session_id"],
            refresh_token=payload.get("refresh_token"),
            expires_at=payload.get("expires_at"),
            authenticated_at=payload["authenticated_at"],
        )

    def is_within(self, maximum_age_hours: int) -> bool:
        authenticated_at = datetime.fromisoformat(self.authenticated_at)
        if authenticated_at.tzinfo is None:
            authenticated_at = authenticated_at.replace(tzinfo=UTC)
        return authenticated_at + timedelta(hours=maximum_age_hours) > datetime.now(UTC)

    def access_only(self) -> ComdirectSessionState:
        return ComdirectSessionState(
            access_token=self.access_token,
            session_id=self.session_id,
            refresh_token=None,
            expires_at=self.expires_at,
            authenticated_at=self.authenticated_at,
        )


@dataclass(frozen=True, slots=True)
class TransactionPage:
    account_external_id: str
    offset: int
    next_offset: int
    transactions: tuple[NormalizedTransaction, ...]


class ComdirectProvider(AccountProvider):
    kind = "comdirect"
    token_url = "https://api.comdirect.de/oauth/token"
    revoke_url = "https://api.comdirect.de/oauth/revoke"
    api_url = "https://api.comdirect.de/api"
    _allowed_request_paths: ClassVar[set[tuple[str, str]]] = {
        ("post", "/oauth/token"),
        ("delete", "/oauth/revoke"),
        ("get", "/api/session/clients/user/v1/sessions"),
    }

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        *,
        session: Any = None,
        push_tan_callback: Callable[[], str] | None = None,
        photo_tan_callback: Callable[[bytes], str] | None = None,
        sms_tan_callback: Callable[[], str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.credentials = credentials or {}
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.push_tan_callback = push_tan_callback or self._push_tan
        self.photo_tan_callback = photo_tan_callback
        self.sms_tan_callback = sms_tan_callback
        self.timeout_seconds = timeout_seconds
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: datetime | None = None
        self.session_id: str | None = None
        self.authenticated_at: datetime | None = None
        self.allow_token_refresh = True

    @classmethod
    def from_config(
        cls,
        config: ComdirectConnectionConfig,
        secret_store: SecretStore,
        password: str,
        **kwargs: Any,
    ) -> ComdirectProvider:
        names = {
            "username": config.username_secret,
            "client_id": config.client_id_secret,
            "client_secret": config.client_secret_secret,
        }
        credentials = {
            field: secret_store.get_keyring(secret_name) or ""
            for field, secret_name in names.items()
        }
        missing = [names[field] for field, value in credentials.items() if not value]
        if missing:
            raise ComdirectAuthenticationError(
                "Fehlende Comdirect-Secretreferenzen: " + ", ".join(missing)
            )
        if not password:
            raise ComdirectAuthenticationError("Comdirect PIN missing")
        credentials["password"] = password
        kwargs.setdefault("timeout_seconds", config.timeout_seconds)
        return cls(credentials, **kwargs)

    @staticmethod
    def _push_tan() -> str:
        input("Confirm PushTAN in the Comdirect app and press Enter. ")
        return ""

    def export_session(self) -> ComdirectSessionState:
        if not self.access_token or not self.session_id:
            raise ComdirectAuthenticationError("No active Comdirect session")
        return ComdirectSessionState(
            access_token=self.access_token,
            session_id=self.session_id,
            refresh_token=self.refresh_token,
            expires_at=(self.expires_at.astimezone(UTC).isoformat() if self.expires_at else None),
            authenticated_at=(
                self.authenticated_at.astimezone(UTC).isoformat()
                if self.authenticated_at
                else datetime.now(UTC).isoformat()
            ),
        )

    def restore_session(self, state: ComdirectSessionState) -> None:
        self.access_token = state.access_token
        self.session_id = state.session_id
        self.refresh_token = state.refresh_token
        self.expires_at = datetime.fromisoformat(state.expires_at) if state.expires_at else None
        self.authenticated_at = datetime.fromisoformat(state.authenticated_at)

    def _request_info(self) -> str:
        if not self.session_id:
            raise ComdirectAuthenticationError("No active Comdirect session")
        return json.dumps(
            {
                "clientRequestId": {
                    "sessionId": self.session_id,
                    "requestId": str(uuid.uuid4()),
                }
            }
        )

    def _headers(self, include_request_info: bool = True) -> dict[str, str]:
        if not self.access_token:
            raise ComdirectAuthenticationError("Nicht angemeldet")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        if include_request_info:
            headers["x-http-request-info"] = self._request_info()
        return headers

    def login(self) -> ComdirectSessionState:
        if self.session is None:
            raise RuntimeError("requests is required for Comdirect")
        primary = self._send(
            "post",
            self.token_url,
            "OAuth-Anmeldung",
            data={**self.credentials, "grant_type": "password"},
            headers={"Accept": "application/json"},
        )
        primary_token = primary.json()["access_token"]
        initial_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {primary_token}",
            "x-http-request-info": json.dumps(
                {
                    "clientRequestId": {
                        "sessionId": str(uuid.uuid4()),
                        "requestId": str(uuid.uuid4()),
                    }
                }
            ),
        }
        session_response = self._send(
            "get",
            f"{self.api_url}/session/clients/user/v1/sessions",
            "Session-Erstellung",
            headers=initial_headers,
        )
        try:
            self.session_id = session_response.json()[0]["identifier"]
        except (IndexError, KeyError, TypeError) as error:
            raise ComdirectAuthenticationError("Invalid session response") from error
        self._complete_2fa(primary_token)
        token_response = self._send(
            "post",
            self.token_url,
            "Secondary OAuth token",
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "grant_type": "cd_secondary",
                "token": primary_token,
            },
            headers={"Accept": "application/json"},
        )
        self._apply_token(token_response.json())
        self.authenticated_at = datetime.now(UTC)
        return self.export_session()

    def refresh(self) -> ComdirectSessionState:
        if not self.refresh_token:
            raise ComdirectAuthenticationError("No renewable Comdirect session")
        response = self._send(
            "post",
            self.token_url,
            "OAuth-Token-Erneuerung",
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            headers={"Accept": "application/json"},
        )
        self._apply_token(response.json())
        return self.export_session()

    def _apply_token(self, payload: dict[str, Any]) -> None:
        try:
            self.access_token = payload["access_token"]
        except KeyError as error:
            raise ComdirectAuthenticationError("OAuth response contains no access token") from error
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        expires_in = payload.get("expires_in")
        self.expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(expires_in))
            if expires_in is not None
            else None
        )

    def ensure_authenticated(self, *, interactive: bool = True) -> None:
        if self.access_token and not self._is_expired():
            return
        if self.allow_token_refresh and self.refresh_token and self.session_id:
            try:
                self.refresh()
                return
            except ComdirectError:
                self.access_token = None
        if not interactive:
            raise ComdirectAuthenticationError(
                "Comdirect authentication required; run account-eval auth comdirect locally"
            )
        self.login()

    def _is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC) + timedelta(seconds=30)

    def _complete_2fa(self, token: str) -> None:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-http-request-info": self._request_info(),
        }
        response = self._send(
            "post",
            (f"{self.api_url}/session/clients/user/v1/sessions/{self.session_id}/validate"),
            "2FA-Initialisierung",
            expected=(201,),
            headers=headers,
            json={
                "identifier": self.session_id,
                "sessionTanActive": True,
                "activated2FA": True,
            },
        )
        try:
            info = json.loads(response.headers.get("x-once-authentication-info", "{}"))
        except json.JSONDecodeError as error:
            raise ComdirectAuthenticationError("Invalid 2FA challenge") from error
        kind = info.get("typ")
        if kind == "P_TAN_PUSH":
            tan = self.push_tan_callback()
        elif kind == "P_TAN" and self.photo_tan_callback:
            tan = self.photo_tan_callback(base64.b64decode(info["challenge"]))
        elif kind == "M_TAN" and self.sms_tan_callback:
            tan = self.sms_tan_callback()
        else:
            raise ComdirectAuthenticationError(f"Unsupported or unconfigured TAN method: {kind}")
        headers["x-once-authentication-info"] = json.dumps({"id": info["id"]})
        headers["x-once-authentication"] = tan
        self._send(
            "patch",
            (f"{self.api_url}/session/clients/user/v1/sessions/{self.session_id}"),
            "2FA confirmation",
            headers=headers,
            json={
                "identifier": self.session_id,
                "sessionTanActive": True,
                "activated2FA": True,
            },
        )

    def _send(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Any:
        self._validate_request(method, url)
        kwargs.setdefault("timeout", self.timeout_seconds)
        kwargs.setdefault("allow_redirects", False)
        try:
            response = getattr(self.session, method)(url, **kwargs)
        except Exception as error:
            if requests and isinstance(
                error,
                (requests.Timeout, requests.ConnectionError),
            ):
                raise ComdirectTemporaryError(f"{operation} temporarily unreachable") from error
            raise
        self._check(response, operation, expected)
        return response

    @classmethod
    def _validate_request(cls, method: str, url: str) -> None:
        parsed = urlsplit(url)
        path = parsed.path
        allowed = (method.casefold(), path) in cls._allowed_request_paths
        allowed = allowed or (
            method.casefold() in {"post", "patch"}
            and path.startswith("/api/session/clients/user/v1/sessions/")
            and (
                path.endswith("/validate")
                or "/" not in path.removeprefix("/api/session/clients/user/v1/sessions/")
            )
        )
        allowed = allowed or (
            method.casefold() == "get"
            and (
                path == "/api/banking/clients/user/v2/accounts/balances"
                or (path.startswith("/api/banking/v1/accounts/") and path.endswith("/transactions"))
            )
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.comdirect.de"
            or parsed.query
            or parsed.fragment
            or not allowed
        ):
            raise ComdirectAPIError("Nicht erlaubte Comdirect-Anfrage")

    @staticmethod
    def _check(
        response: Any,
        operation: str,
        expected: tuple[int, ...] = (200,),
    ) -> None:
        status_code = int(response.status_code)
        if status_code in expected:
            return
        if status_code == 429:
            raise ComdirectRateLimitError(f"{operation} was rate limited (HTTP 429)")
        if status_code >= 500:
            raise ComdirectTemporaryError(f"{operation} temporarily failed (HTTP {status_code})")
        if status_code in (401, 403):
            raise ComdirectAuthenticationError(
                f"{operation} erfordert neue Authentisierung (HTTP {status_code})"
            )
        raise ComdirectAPIError(f"{operation} fehlgeschlagen (HTTP {status_code})")

    def list_accounts(self, *, interactive: bool = True) -> list[dict[str, Any]]:
        self.ensure_authenticated(interactive=interactive)
        response = self._send(
            "get",
            f"{self.api_url}/banking/clients/user/v2/accounts/balances",
            "Kontenabruf",
            headers=self._headers(),
        )
        return [
            self.balance_record(record)
            for record in response.json().get("values", [])
            if isinstance(record, dict)
        ]

    @classmethod
    def balance_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        nested = record.get("account")
        if not isinstance(nested, dict):
            return dict(record)
        merged = dict(nested)
        for key, value in record.items():
            if key == "account" or value is None:
                continue
            merged[key] = value
        return merged

    _CHECKING_ACCOUNT_TYPE_KEYS = frozenset(
        {
            "ca",  # Comdirect balances API: Girokonto
            "giro",
            "girokonto",
            "checking",
        }
    )
    _SAVINGS_ACCOUNT_TYPE_KEYS = frozenset(
        {
            "callmoney",
            "das",  # Comdirect balances API: Tagesgeld / Direct Access Savings
            "savings",
            "tagesgeld",
        }
    )

    @staticmethod
    def _token_from_account_type_value(value: Any) -> str:
        if isinstance(value, dict):
            for field in ("key", "text", "value"):
                if isinstance(candidate := value.get(field), str) and candidate.strip():
                    return candidate.strip()
            return ""
        return str(value or "").strip()

    @classmethod
    def account_type_resolution(cls, record: dict[str, Any]) -> tuple[str, str]:
        top_level = cls._token_from_account_type_value(record.get("accountType"))
        if top_level:
            return top_level, "top_level"
        nested = record.get("account")
        if isinstance(nested, dict):
            nested_token = cls._token_from_account_type_value(nested.get("accountType"))
            if nested_token:
                return nested_token, "nested.account"
        return "", "missing"

    @classmethod
    def _account_type_token(cls, account: dict[str, Any]) -> str:
        token, _ = cls.account_type_resolution(account)
        if token:
            return token
        return cls._token_from_account_type_value(cls.balance_record(account).get("accountType"))

    @classmethod
    def account_type(cls, account: dict[str, Any]) -> AccountType | None:
        normalized = "".join(
            character
            for character in cls._account_type_token(account).casefold()
            if character.isalnum()
        )
        if normalized in cls._CHECKING_ACCOUNT_TYPE_KEYS:
            return AccountType.CHECKING
        if normalized in cls._SAVINGS_ACCOUNT_TYPE_KEYS:
            return AccountType.SAVINGS
        return None

    def logout(self) -> None:
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None
        self.session_id = None
        self.authenticated_at = None

    def revoke(self) -> None:
        if not self.access_token:
            self.logout()
            return
        try:
            self._send(
                "delete",
                self.revoke_url,
                "OAuth-Widerruf",
                expected=(200, 204),
                headers=self._headers(),
            )
        finally:
            self.logout()

    def iter_transaction_pages(
        self,
        *,
        accounts: list[dict[str, Any]] | None = None,
        account_external_ids: set[str] | None = None,
        booking_from: date | None = None,
        booking_to: date | None = None,
        start_offsets: dict[str, int] | None = None,
        interactive: bool = True,
    ) -> Iterator[TransactionPage]:
        offsets = start_offsets or {}
        remote_accounts = (
            accounts if accounts is not None else self.list_accounts(interactive=interactive)
        )
        for account in remote_accounts:
            account = self.balance_record(account)
            if self.account_type(account) is None:
                continue
            account_id = account["accountId"]
            if account_external_ids is not None and account_id not in account_external_ids:
                continue
            offset = int(offsets.get(account_id, 0))
            while True:
                params: dict[str, Any] = {
                    "transactionState": "BOOKED",
                    "paging-first": offset,
                }
                if booking_from:
                    params["min-bookingDate"] = booking_from.isoformat()
                if booking_to:
                    params["max-bookingDate"] = booking_to.isoformat()
                response = self._send(
                    "get",
                    (f"{self.api_url}/banking/v1/accounts/{account_id}/transactions"),
                    "Umsatzabruf",
                    params=params,
                    headers=self._headers(),
                )
                payload = response.json()
                values = payload.get("values", [])
                if not values:
                    break
                transactions = tuple(
                    self._normalize_transaction(account_id, value) for value in values
                )
                next_offset = offset + len(values)
                yield TransactionPage(
                    account_external_id=account_id,
                    offset=offset,
                    next_offset=next_offset,
                    transactions=transactions,
                )
                matches = (payload.get("paging") or {}).get("matches")
                offset = next_offset
                if matches is not None and offset >= int(matches):
                    break

    def iter_transactions(self) -> Iterable[NormalizedTransaction]:
        for page in self.iter_transaction_pages():
            yield from page.transactions

    def _normalize_transaction(
        self,
        account_id: str,
        value: dict[str, Any],
    ) -> NormalizedTransaction:
        amount = value["amount"]
        return NormalizedTransaction(
            external_id=(value.get("transactionId") or self._stable_id(account_id, value)),
            account_external_id=account_id,
            booking_date=date.fromisoformat(value["bookingDate"]),
            valuta_date=(
                date.fromisoformat(value["valutaDate"]) if value.get("valutaDate") else None
            ),
            amount=Decimal(str(amount["value"])),
            currency=amount.get("unit", "EUR"),
            purpose=value.get("remittanceInfo") or "",
            reference=value.get("reference"),
            counterparty=self._counterparty(value),
            raw=value,
            booking_status=self._booking_status(value),
        )

    @staticmethod
    def _booking_status(value: dict[str, Any]) -> TransactionStatus:
        raw = str(value.get("bookingStatus") or value.get("status") or "booked")
        normalized = "".join(character for character in raw.casefold() if character.isalnum())
        if normalized in {
            "pending",
            "notbooked",
            "preliminary",
            "vorgemerkt",
        }:
            return TransactionStatus.PENDING
        if normalized in {"reversed", "cancelled", "storniert"}:
            return TransactionStatus.REVERSED
        if normalized in {"corrected", "korrigiert"}:
            return TransactionStatus.CORRECTED
        return TransactionStatus.BOOKED

    @staticmethod
    def _stable_id(account_id: str, value: dict[str, Any]) -> str:
        identity = {
            "account_id": account_id,
            "booking_date": value.get("bookingDate"),
            "valuta_date": value.get("valutaDate"),
            "amount": value.get("amount"),
            "reference": value.get("reference"),
            "remittance_info": value.get("remittanceInfo"),
            "counterparty": ComdirectProvider._counterparty(value),
        }
        return hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _counterparty(value: dict[str, Any]) -> str | None:
        for key in ("remitter", "creditor", "debtor", "deptor"):
            party = value.get(key)
            if party and party.get("holderName"):
                return party["holderName"]
        return None
