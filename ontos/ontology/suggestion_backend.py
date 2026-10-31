"""Sovereign-only backend contract for LLM-assisted ontology suggestions."""

from __future__ import annotations

from typing import Any, Protocol


class SuggestionBackend(Protocol):
    """Structured-output backend used only for ontology suggestions."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def sovereign(self) -> bool: ...

    async def structured_suggest(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class SuggestionBackendRefusedError(RuntimeError):
    """An unapproved backend was supplied for sensitive candidate evidence."""


def require_sovereign_suggestion_backend(backend: object) -> None:
    """Fail closed before passing rejected candidate evidence to a model."""
    if getattr(backend, "sovereign", None) is not True:
        raise SuggestionBackendRefusedError(
            "Ontology suggestions require an explicitly sovereign LLM backend"
        )

    method = getattr(backend, "structured_suggest", None)
    if not callable(method):
        raise SuggestionBackendRefusedError(
            "Ontology suggestion backend lacks structured output support"
        )
