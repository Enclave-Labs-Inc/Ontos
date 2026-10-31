"""Endpoint sovereignty checks for ontology suggestions."""

import pytest

from ontos.ontology.suggestion_endpoint import (
    UntrustedSuggestionEndpointError,
    require_local_ollama_endpoint,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
        ("http://[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_local_endpoints_are_canonicalized(url: str, expected: str) -> None:
    assert require_local_ollama_endpoint(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:11434",
        "http://example.com:11434",
        "http://localhost.evil.com:11434",
        "http://10.0.0.5:11434",
        "http://192.168.1.5:11434",
        "http://0.0.0.0:11434",
        "http://127.0.0.2:11434",
        "http://user:pass@localhost:11434",
        "http://localhost:11434/api/chat",
        "http://localhost:11434?redirect=external",
        "http://localhost:11434#fragment",
        "http://localhost",
        "http://localhost:0",
        "http://localhost:99999",
        "unix:///var/run/ollama",
    ],
)
def test_untrusted_endpoints_are_refused(url: str) -> None:
    with pytest.raises(UntrustedSuggestionEndpointError):
        require_local_ollama_endpoint(url)
