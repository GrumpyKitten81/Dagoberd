from __future__ import annotations

import socket

import httpx
import pytest
from pydantic import ValidationError

from account_eval.config import AppConfig, LLMConfig
from account_eval.llm.openai_compatible import LLMProviderError, OpenAICompatibleProvider


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.2.3", "[::1]", "[0:0:0:0:0:0:0:1]"])
def test_local_mode_accepts_only_loopback_literals(host):
    assert LLMConfig(enabled=True, model="test", base_url=f"http://{host}:1234/v1").enabled


@pytest.mark.parametrize("host", ["example.invalid", "192.168.1.2", "[::ffff:127.0.0.1]", "127.1"])
def test_remote_endpoint_requires_cloud_mode_before_client_creation(host, monkeypatch):
    monkeypatch.setattr(
        "account_eval.llm.openai_compatible.OpenAI",
        lambda **_: pytest.fail("Invalid endpoint must not create a client"),
    )
    with pytest.raises(ValidationError, match="loopback"):
        LLMConfig(enabled=True, model="test", base_url=f"http://{host}/v1")


@pytest.mark.parametrize("resolved", [["127.0.0.1", "::1"], ["127.0.0.1", "192.0.2.1"], []])
def test_localhost_resolution_must_be_entirely_loopback(monkeypatch, resolved):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 1234)) for address in resolved
        ],
    )
    if resolved and all(address in {"127.0.0.1", "::1"} for address in resolved):
        assert LLMConfig(enabled=True, model="test", base_url="http://localhost:1234/v1").enabled
    else:
        with pytest.raises(ValidationError, match="loopback"):
            LLMConfig(enabled=True, model="test", base_url="http://localhost:1234/v1")


def test_unresolvable_localhost_fails_before_network_access(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror("test resolution failure")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValidationError, match="could not be resolved"):
        LLMConfig(enabled=True, model="test", base_url="http://localhost/v1")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "http://user:secret@127.0.0.1/v1",
        "http://127.0.0.1/v1?key=secret",
        "http://127.0.0.1/v1#fragment",
    ],
)
def test_endpoint_rejects_embedded_credentials_and_non_http_urls(url):
    with pytest.raises(ValidationError, match="HTTP"):
        LLMConfig(enabled=True, model="test", base_url=url)


def test_mutated_config_cannot_bypass_provider_validation():
    config = LLMConfig(enabled=True, model="test")
    config.base_url = "https://example.invalid/v1"
    with pytest.raises(ValidationError, match="loopback"):
        OpenAICompatibleProvider(config, client=object())


def test_invalid_endpoint_credentials_are_absent_from_configuration_errors():
    with pytest.raises(ValidationError) as caught:
        AppConfig.model_validate(
            {
                "llm": {
                    "enabled": True,
                    "model": "test",
                    "base_url": "http://user:synthetic-password@127.0.0.1/v1",
                }
            }
        )
    assert "synthetic-password" not in str(caught.value)


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_provider_does_not_follow_redirects_or_use_local_environment_proxy(monkeypatch, mode):
    from openai import DefaultHttpxClient

    requests = []
    options = {}

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://example.invalid/stolen"})

    def client_factory(**kwargs):
        options.update(kwargs)
        return DefaultHttpxClient(transport=httpx.MockTransport(redirect), **kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr("account_eval.llm.openai_compatible.DefaultHttpxClient", client_factory)
    provider = OpenAICompatibleProvider(
        LLMConfig(enabled=True, model="test", mode=mode, allow_cloud=mode == "cloud"),
        api_key="synthetic-key",
    )
    try:
        with pytest.raises(LLMProviderError, match="connection test failed"):
            provider.test_connection()
        assert len(requests) == 1
        assert requests[0].url.host == "127.0.0.1"
        assert options == {"follow_redirects": False, "trust_env": mode != "local"}
    finally:
        provider.client.close()
