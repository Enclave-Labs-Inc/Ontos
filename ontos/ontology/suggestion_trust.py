"""Production trust boundary for LLM-assisted ontology suggestions.

Only explicitly approved sovereign implementations may receive evidence.
A backend's self-declared sovereign marker is necessary, not sufficient.

This is the single place the checks run (#90). Approval returns an
`ApprovedSuggestionBackend` token that the pipeline and request layers
accept instead of re-checking the backend, and that can only be minted
here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ontos.llm.ollama import OllamaBackend
from ontos.ontology.suggestion_backend import (
    SuggestionBackend,
    SuggestionBackendRefusedError,
    require_sovereign_suggestion_backend,
)

# Exact classes allowed to receive candidate evidence. Exact identity (not
# isinstance) so a subclass or spoofed object can't claim approval. A future
# sovereign Scribe adapter is added here after review.
APPROVED_SUGGESTION_BACKENDS: tuple[type, ...] = (OllamaBackend,)

_SEAL = object()


@dataclass(frozen=True)
class ApprovedSuggestionBackend:
    """Proof that a backend passed the trust gate; minted only by the gate."""

    backend: SuggestionBackend
    endpoint: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise SuggestionBackendRefusedError(
                "Approved suggestion backends are issued only by the trust gate"
            )

    @property
    def id(self) -> str:
        return self.backend.id

    @property
    def version(self) -> str:
        return self.backend.version

    async def resolve_suggestion_version(self) -> str:
        resolve = getattr(self.backend, "resolve_suggestion_version", None)
        if resolve is None:
            return self.backend.version
        version: str = await resolve()
        return version

    async def structured_suggest(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.backend.structured_suggest(system=system, user=user, schema=schema)


def require_approved_suggestion_backend(
    backend: SuggestionBackend | ApprovedSuggestionBackend,
) -> ApprovedSuggestionBackend:
    """Verify an approved backend and its transport before evidence access."""
    if isinstance(backend, ApprovedSuggestionBackend):
        return backend

    if type(backend) not in APPROVED_SUGGESTION_BACKENDS:
        raise SuggestionBackendRefusedError(
            "Ontology suggestions require an approved sovereign backend"
        )

    require_sovereign_suggestion_backend(backend)

    attest = getattr(backend, "attest_local_transport", None)
    if not callable(attest):
        raise SuggestionBackendRefusedError(
            "Ontology suggestion backend cannot attest a local transport"
        )
    # Rejects remote endpoints and injected clients before any candidate
    # payload is prepared.
    endpoint = attest()

    return ApprovedSuggestionBackend(backend=backend, endpoint=endpoint, _seal=_SEAL)
