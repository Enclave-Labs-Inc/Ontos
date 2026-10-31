"""Fail-closed Ollama endpoint validation for ontology suggestions."""

from __future__ import annotations

from urllib.parse import urlsplit


class UntrustedSuggestionEndpointError(ValueError):
    """The suggestion endpoint is outside the trusted local boundary."""


def require_local_ollama_endpoint(base_url: str) -> str:
    """Return a canonical loopback URL or refuse the endpoint.

    Only direct HTTP loopback connections are supported here.
    Callers must also disable environment proxies and redirects.
    """
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UntrustedSuggestionEndpointError("Invalid Ollama suggestion endpoint") from exc

    if (
        parsed.scheme != "http"
        or host not in {"localhost", "127.0.0.1", "::1"}
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise UntrustedSuggestionEndpointError(
            "Ontology suggestions require a trusted loopback Ollama endpoint"
        )

    canonical_host = "[::1]" if host == "::1" else "127.0.0.1"
    return f"http://{canonical_host}:{port}"
