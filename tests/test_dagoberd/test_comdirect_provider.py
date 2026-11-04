import json
from datetime import UTC, date, datetime, timedelta

import pytest
import requests

from dagoberd.config import ComdirectConnectionConfig
from dagoberd.providers.comdirect import (
    ComdirectAPIError,
    ComdirectAuthenticationError,
    ComdirectProvider,
    ComdirectRateLimitError,
    ComdirectSessionState,
    ComdirectTemporaryError,
)


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = headers or {}

    def json(self):
        return self._payload


class Session:
    def __init__(self):
        self.responses = [
            Response(200, {"values": [{"accountId": "acc-1", "accountType": "GIRO"}]}),
            Response(
                200,
                {
                    "paging": {"index": 0, "matches": 1},
                    "values": [
                        {
                            "bookingDate": "2026-07-01",
                            "valutaDate": "2026-07-01",
                            "amount": {"value": "12.34", "unit": "EUR"},
                            "remittanceInfo": "Test",
                            "reference": "ref-1",
                        }
                    ],
                },
            ),
        ]
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class AuthenticationSession:
    def __init__(self, challenge_type="P_TAN_PUSH"):
        self.challenge_type = challenge_type
        self.posts = []
        self.patches = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if len(self.posts) == 1:
            return Response(200, {"access_token": "primary"})
        if len(self.posts) == 2:
            challenge = {
                "id": "challenge-id",
                "typ": self.challenge_type,
            }
            if self.challenge_type == "P_TAN":
                challenge["challenge"] = "aW1hZ2U="
            return Response(
                201,
                {},
                {"x-once-authentication-info": (json.dumps(challenge))},
            )
        return Response(
            200,
            {
                "access_token": "secondary",
                "refresh_token": "refresh",
                "expires_in": 300,
            },
        )

    def get(self, url, **kwargs):
        return Response(200, [{"identifier": "session"}])

    def patch(self, url, **kwargs):
        self.patches.append((url, kwargs))
        return Response(200, {})


def test_comdirect_provider_paginates_booked_transactions_without_bank_write():
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=Session(),
    )
    provider.access_token = "token"
    provider.session_id = "session"
    transactions = list(provider.iter_transactions())
    assert len(transactions) == 1
    assert len(transactions[0].external_id) == 64
    assert transactions[0].reference == "ref-1"
    assert len(provider.session.calls) == 2
    provider.logout()
    assert provider.access_token is None


def test_transaction_page_passes_booking_date_boundaries():
    session = Session()
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=session,
    )
    provider.access_token = "token"
    provider.session_id = "session"

    list(
        provider.iter_transaction_pages(
            booking_from=date(2026, 1, 1),
            booking_to=date(2026, 6, 30),
        )
    )

    params = session.calls[1][1]["params"]
    assert params["min-bookingDate"] == "2026-01-01"
    assert params["max-bookingDate"] == "2026-06-30"


def test_comdirect_normalizes_pending_booking_status():
    provider = ComdirectProvider(
        {
            "username": "x",
            "password": "x",
            "client_id": "x",
            "client_secret": "x",
        },
        session=Session(),
    )

    transaction = provider._normalize_transaction(
        "account",
        {
            "transactionId": "pending-1",
            "bookingDate": "2026-07-01",
            "amount": {"value": "10.00", "unit": "EUR"},
            "bookingStatus": "NOTBOOKED",
        },
    )

    assert transaction.booking_status == "pending"


def test_fallback_identity_does_not_reuse_a_repeated_reference():
    first = {
        "bookingDate": "2026-01-01",
        "amount": {"value": "10.00", "unit": "EUR"},
        "reference": "repeat",
        "remittanceInfo": "First",
    }
    second = {
        **first,
        "bookingDate": "2026-02-01",
        "remittanceInfo": "Second",
    }

    assert ComdirectProvider._stable_id(
        "account",
        first,
    ) != ComdirectProvider._stable_id("account", second)


def test_session_state_round_trip_and_expiry():
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=Session(),
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    state = ComdirectSessionState(
        access_token="access",
        session_id="session",
        refresh_token="refresh",
        expires_at=expires_at.isoformat(),
        authenticated_at=datetime.now(UTC).isoformat(),
    )

    provider.restore_session(ComdirectSessionState.from_json(state.to_json()))

    assert provider.export_session() == state
    assert provider._is_expired() is False


def test_http_errors_are_typed_and_do_not_expose_response_body():
    response = Response(429, {"error": "secret response"})
    response.text = "secret response"

    with pytest.raises(ComdirectRateLimitError) as error:
        ComdirectProvider._check(response, "Umsatzabruf")

    assert "secret response" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ComdirectAuthenticationError),
        (403, ComdirectAuthenticationError),
        (500, ComdirectTemporaryError),
        (503, ComdirectTemporaryError),
        (400, ComdirectAPIError),
    ],
)
def test_http_statuses_have_stable_error_types(status, error_type):
    with pytest.raises(error_type):
        ComdirectProvider._check(Response(status, {}), "request")


def test_noninteractive_authentication_never_prompts():
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=Session(),
    )

    with pytest.raises(
        ComdirectAuthenticationError,
        match="auth comdirect",
    ):
        provider.ensure_authenticated(interactive=False)


def test_network_timeout_is_retryable_and_sanitized():
    class TimeoutSession:
        def get(self, url, **kwargs):
            raise requests.Timeout("secret transport details")

    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=TimeoutSession(),
    )
    provider.access_token = "token"
    provider.session_id = "session"

    with pytest.raises(ComdirectTemporaryError) as error:
        provider.list_accounts(interactive=False)

    assert "secret transport details" not in str(error.value)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "https://api.comdirect.de/api/brokerage/v3/depots"),
        ("post", "https://api.comdirect.de/api/brokerage/v3/orders"),
        ("get", "https://api.comdirect.de/api/messages/v1/documents"),
        ("get", "https://example.test/api/banking/clients/user/v2/accounts/balances"),
        ("get", "http://api.comdirect.de/api/banking/clients/user/v2/accounts/balances"),
    ],
)
def test_transport_blocks_any_non_account_request_before_network_access(method, url):
    provider = ComdirectProvider(session=Session())

    with pytest.raises(ComdirectAPIError, match="Nicht erlaubte"):
        provider._send(method, url, "blocked")

    assert provider.session.calls == []


def test_transport_disables_redirects_for_allowed_account_requests():
    session = Session()
    provider = ComdirectProvider(session=session)
    provider.access_token = "token"
    provider.session_id = "session"

    provider.list_accounts(interactive=False)

    assert session.calls[0][1]["allow_redirects"] is False


def test_transaction_requests_skip_card_and_depot_products():
    class MixedProductsSession:
        def __init__(self):
            self.calls = []
            self.responses = [
                Response(
                    200,
                    {
                        "values": [
                            {"accountId": "giro", "accountType": "GIRO"},
                            {"accountId": "visa", "accountType": "CREDIT_CARD"},
                            {"accountId": "depot", "accountType": "DEPOT"},
                        ]
                    },
                ),
                Response(200, {"paging": {"matches": 0}, "values": []}),
            ]

        def get(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.responses.pop(0)

    session = MixedProductsSession()
    provider = ComdirectProvider(session=session)
    provider.access_token = "token"
    provider.session_id = "session"

    assert list(provider.iter_transactions()) == []
    assert len(session.calls) == 2
    assert "/accounts/giro/transactions" in session.calls[1][0][0]


def test_session_state_without_authentication_time_is_rejected():
    with pytest.raises(KeyError):
        ComdirectSessionState.from_json(
            json.dumps(
                {
                    "access_token": "access",
                    "session_id": "session",
                    "refresh_token": "refresh",
                    "expires_at": None,
                }
            )
        )


def test_configured_credentials_are_read_only_from_keyring():
    class KeyringOnlySecrets:
        def get(self, name):
            raise AssertionError(f"environment fallback used for {name}")

        def get_keyring(self, name):
            return {
                "comdirect.private.username": "user",
                "comdirect.private.client-id": "client",
                "comdirect.private.client-secret": "secret",
            }.get(name)

    provider = ComdirectProvider.from_config(
        ComdirectConnectionConfig(
            username_secret="comdirect.private.username",
            client_id_secret="comdirect.private.client-id",
            client_secret_secret="comdirect.private.client-secret",
        ),
        KeyringOnlySecrets(),
        "interactive-pin",
        session=Session(),
    )

    assert provider.credentials == {
        "username": "user",
        "client_id": "client",
        "client_secret": "secret",
        "password": "interactive-pin",
    }


def test_oauth_and_push_tan_flow_returns_persistable_session_state():
    session = AuthenticationSession()
    callback_calls = []
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=session,
        push_tan_callback=lambda: callback_calls.append("push") or "",
    )

    state = provider.login()

    assert state.access_token == "secondary"
    assert state.refresh_token == "refresh"
    assert state.session_id == "session"
    assert callback_calls == ["push"]
    assert session.patches[0][1]["headers"]["x-once-authentication"] == ""


def test_expired_session_is_refreshed_without_interactive_2fa():
    class RefreshSession:
        def post(self, url, **kwargs):
            assert kwargs["data"]["grant_type"] == "refresh_token"
            return Response(
                200,
                {
                    "access_token": "renewed",
                    "refresh_token": "next-refresh",
                    "expires_in": 300,
                },
            )

    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=RefreshSession(),
    )
    provider.restore_session(
        ComdirectSessionState(
            access_token="expired",
            session_id="session",
            refresh_token="refresh",
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            authenticated_at=datetime.now(UTC).isoformat(),
        )
    )

    provider.ensure_authenticated(interactive=False)

    assert provider.access_token == "renewed"
    assert provider.refresh_token == "next-refresh"


@pytest.mark.parametrize(
    ("challenge_type", "callback_name"),
    [("P_TAN", "photo"), ("M_TAN", "sms")],
)
def test_photo_and_sms_tan_callbacks_are_separate_strategies(
    challenge_type,
    callback_name,
):
    calls = []
    provider = ComdirectProvider(
        {"username": "x", "password": "x", "client_id": "x", "client_secret": "x"},
        session=AuthenticationSession(challenge_type),
        photo_tan_callback=lambda challenge: calls.append(("photo", challenge)) or "123456",
        sms_tan_callback=lambda: calls.append(("sms", None)) or "654321",
    )

    provider.login()

    assert calls[0][0] == callback_name


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ({"accountType": "GIRO"}, "checking"),
        ({"accountType": "CALL_MONEY"}, "savings"),
        (
            {"accountType": {"key": "GIRO", "text": "Girokonto"}},
            "checking",
        ),
        (
            {"accountType": {"key": "CALL_MONEY", "text": "Tagesgeld"}},
            "savings",
        ),
        (
            {"accountType": {"key": "CA", "text": "Girokonto"}},
            "checking",
        ),
        (
            {"accountType": {"key": "DAS", "text": "Tagesgeld"}},
            "savings",
        ),
    ],
)
def test_account_type_accepts_enum_objects(account, expected):
    mapped = ComdirectProvider.account_type(account)
    assert mapped is not None
    assert mapped.value == expected


def test_balance_record_merges_nested_account_metadata():
    record = {
        "accountId": "top-id",
        "balance": {"value": "42.00", "unit": "EUR"},
        "account": {
            "accountId": "nested-id",
            "accountDisplayId": "Giro 1234",
            "accountType": {"key": "CA", "text": "Girokonto"},
        },
    }

    merged = ComdirectProvider.balance_record(record)

    assert merged["accountId"] == "top-id"
    assert merged["accountDisplayId"] == "Giro 1234"
    assert merged["balance"] == {"value": "42.00", "unit": "EUR"}
    assert ComdirectProvider.account_type(record) == ComdirectProvider.account_type(merged)


@pytest.mark.parametrize(
    ("record", "token", "source"),
    [
        (
            {"accountType": {"key": "CA", "text": "Girokonto"}},
            "CA",
            "top_level",
        ),
        (
            {
                "accountId": "id-1",
                "account": {"accountType": {"key": "DAS", "text": "Tagesgeld"}},
            },
            "DAS",
            "nested.account",
        ),
    ],
)
def test_account_type_resolution_reports_source(record, token, source):
    assert ComdirectProvider.account_type_resolution(record) == (token, source)
