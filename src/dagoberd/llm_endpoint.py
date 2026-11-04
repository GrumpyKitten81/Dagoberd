from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def validate_llm_endpoint(base_url: str, mode: str) -> None:
    try:
        endpoint = urlsplit(base_url)
        host = endpoint.hostname
        port = endpoint.port
    except ValueError as error:
        raise ValueError("Invalid LLM endpoint URL") from error
    if (
        endpoint.scheme not in {"http", "https"}
        or not host
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or any(character.isspace() for character in base_url)
    ):
        raise ValueError(
            "LLM endpoint requires an HTTP(S) URL without credentials, query or fragment"
        )
    if mode != "local":
        return
    if host == "localhost":
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ValueError("Local LLM localhost could not be resolved") from error
        if addresses and all(_is_loopback(str(address[4][0])) for address in addresses):
            return
    elif _is_loopback(host):
        return
    raise ValueError(
        "Local LLM endpoint must use loopback; remote targets require cloud mode and opt-in"
    )


def _is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        and getattr(address, "scope_id", None) is None
        and getattr(address, "ipv4_mapped", None) is None
    )
