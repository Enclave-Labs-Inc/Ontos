"""Production trust boundary for LLM-assisted ontology suggestions.

Only explicitly approved sovereign implementations may receive evidence.
A backend's self-declared sovereign marker is necessary, not sufficient.
"""

from __future__ import annotations

from ontos.llm.ollama import OllamaBackend
from ontos.ontology.suggestion_backend import (
    SuggestionBackend,
    SuggestionBackendRefusedError,
    require_sovereign_suggestion_backend,
)
from ontos.ontology.suggestion_endpoint import (
    UntrustedSuggestionEndpointError,
    require_local_ollama_endpoint,
)


def require_approved_suggestion_backend(
    backend: SuggestionBackend,
) -> OllamaBackend:
    """Verify an approved backend and its transport before evidence access."""
    require_sovereign_suggestion_backend(backend)

    # Exact class identity prevents arbitrary custom implementations
    # or subclasses from claiming an approved sovereign backend.
    if type(backend) is not OllamaBackend:
        raise SuggestionBackendRefusedError(
            "Ontology suggestions require an approved sovereign backend"
        )

    ollama = backend

    # Reject remote endpoints before any candidate payload is prepared.
    require_local_ollama_endpoint(ollama._base_url)

    # Injected clients could enable redirects, proxies or custom transports.
    if ollama._client is not None:
        raise UntrustedSuggestionEndpointError(
            "Injected HTTP clients are forbidden for ontology suggestions"
        )

    return ollama
