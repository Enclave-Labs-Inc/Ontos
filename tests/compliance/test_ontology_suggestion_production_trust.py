"""Compliance tests: production backend approval before evidence access."""

from typing import Any

import httpx
import pytest

from ontos.llm.ollama import OllamaBackend
from ontos.ontology.suggestion_backend import SuggestionBackendRefusedError
from ontos.ontology.suggestion_endpoint import UntrustedSuggestionEndpointError
from ontos.ontology.suggestion_trust import (
    require_approved_suggestion_backend,
)


class SpoofedSovereignBackend:
    id = "ollama:gemma3:12b"
    version = "gemma3:12b"
    sovereign = True

    async def structured_suggest(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        raise AssertionError("Spoofed backend must never receive evidence")


class SimulatedBridgeBackend(SpoofedSovereignBackend):
    sovereign = False


class OllamaSubclass(OllamaBackend):
    """An unapproved subclass must not inherit production approval."""


def test_real_local_ollama_is_approved() -> None:
    backend = OllamaBackend("gemma3:12b")
    assert require_approved_suggestion_backend(backend) is backend


def test_spoofed_sovereign_marker_is_refused() -> None:
    backend = SpoofedSovereignBackend()

    with pytest.raises(SuggestionBackendRefusedError):
        require_approved_suggestion_backend(backend)


def test_unapproved_ollama_subclass_is_refused() -> None:
    backend = OllamaSubclass("gemma3:12b")

    with pytest.raises(SuggestionBackendRefusedError):
        require_approved_suggestion_backend(backend)


def test_bridge_refused_even_with_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")

    with pytest.raises(SuggestionBackendRefusedError):
        require_approved_suggestion_backend(SimulatedBridgeBackend())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://external.example:11434",
        "http://10.0.0.5:11434",
        "http://localhost:11434/api/chat",
    ],
)
def test_untrusted_endpoints_refused(endpoint: str) -> None:
    backend = OllamaBackend("gemma3:12b", base_url=endpoint)

    with pytest.raises(UntrustedSuggestionEndpointError):
        require_approved_suggestion_backend(backend)


@pytest.mark.asyncio
async def test_injected_http_client_is_refused() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Injected client must not receive evidence")
        )
    ) as client:
        backend = OllamaBackend("gemma3:12b", client=client)

        with pytest.raises(UntrustedSuggestionEndpointError):
            require_approved_suggestion_backend(backend)
