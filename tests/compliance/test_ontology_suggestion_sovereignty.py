"""M2b: sovereignty gate for sensitive ontology suggestion evidence."""

from __future__ import annotations

import pytest

from ontos.ontology.suggestion_backend import (
    SuggestionBackendRefusedError,
    require_sovereign_suggestion_backend,
)


class ScriptedSovereignBackend:
    sovereign = True
    id = "scripted:local"
    version = "test-v1"

    async def structured_suggest(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {"suggestions": []}


class BridgeBackend(ScriptedSovereignBackend):
    sovereign = False


class UnknownBackend:
    async def structured_suggest(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {"suggestions": []}


class MissingStructuredOutputBackend:
    sovereign = True


@pytest.mark.parametrize("backend_id", ["openai", "anthropic"])
def test_bridge_backend_refused_even_with_opt_in(
    backend_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")
    monkeypatch.setenv("ONTOS_ENV", "prod")

    backend = BridgeBackend()
    backend.id = backend_id

    with pytest.raises(SuggestionBackendRefusedError, match="sovereign"):
        require_sovereign_suggestion_backend(backend)


def test_unknown_backend_refused() -> None:
    with pytest.raises(SuggestionBackendRefusedError, match="sovereign"):
        require_sovereign_suggestion_backend(UnknownBackend())


def test_missing_structured_output_refused() -> None:
    with pytest.raises(
        SuggestionBackendRefusedError,
        match="structured output",
    ):
        require_sovereign_suggestion_backend(MissingStructuredOutputBackend())


def test_explicitly_sovereign_scripted_backend_accepted() -> None:
    require_sovereign_suggestion_backend(ScriptedSovereignBackend())
