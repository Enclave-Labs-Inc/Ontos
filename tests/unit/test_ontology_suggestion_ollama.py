"""Scripted transport tests for sovereign Ollama suggestions."""

import json
from typing import Any

import httpx
import pytest

from ontos.llm.ollama import OllamaBackend
from ontos.ontology.suggestion_endpoint import (
    UntrustedSuggestionEndpointError,
)


@pytest.mark.asyncio
async def test_suggestions_use_direct_loopback_with_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps({"suggestions": []})}},
        )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    backend = OllamaBackend("gemma3:12b")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"suggestions": {"type": "array"}},
        "required": ["suggestions"],
    }

    result = await backend.structured_suggest(
        "System instructions", "ACL-specific evidence", schema
    )

    assert result == {"suggestions": []}
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:11434/api/chat"

    payload = json.loads(requests[0].content)
    assert payload["format"] == schema
    assert payload["messages"][1]["content"] == "ACL-specific evidence"


@pytest.mark.asyncio
async def test_remote_endpoint_refused_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = OllamaBackend("gemma3:12b", base_url="https://external.example:11434")

    def forbidden_client(**kwargs: Any) -> None:
        raise AssertionError("Network client must not be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)

    with pytest.raises(UntrustedSuggestionEndpointError):
        await backend.structured_suggest("system", "sensitive", {})


@pytest.mark.asyncio
async def test_injected_client_refused() -> None:
    def forbidden_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Injected client must never receive evidence")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_request)) as client:
        backend = OllamaBackend("gemma3:12b", client=client)

        with pytest.raises(UntrustedSuggestionEndpointError):
            await backend.structured_suggest("system", "sensitive", {})


@pytest.mark.asyncio
async def test_redirect_does_not_forward_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://external.example/collect"})

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(redirect), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    backend = OllamaBackend("gemma3:12b")

    with pytest.raises(httpx.HTTPStatusError):
        await backend.structured_suggest("system", "sensitive", {})

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        {"message": {"content": "not valid json"}},
        {"message": {"content": "[]"}},
        {"message": {"content": "null"}},
        {"message": {"content": None}},
        {"message": []},
        ["invalid envelope"],
    ],
)
async def test_malformed_ollama_suggestion_response_is_rejected(
    envelope: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed model content must not escape as a valid proposal."""
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    backend = OllamaBackend("scripted")

    response = await backend.structured_suggest(
        system="system",
        user="authorized evidence",
        schema={"type": "object"},
    )

    assert response == {"proposals": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "not valid JSON",
        '{"message":',
        "",
    ],
)
async def test_malformed_ollama_http_body_is_rejected(
    body: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200 with invalid JSON is rejected as model output."""
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "application/json"},
        )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await OllamaBackend("scripted").structured_suggest(
        system="system",
        user="authorized evidence",
        schema={"type": "object"},
    )

    assert result == {"proposals": None}
