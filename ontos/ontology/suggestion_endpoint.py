"""Compatibility re-export; the policy lives in `ontos.llm.local_endpoint` (#90)."""

from ontos.llm.local_endpoint import (
    UntrustedSuggestionEndpointError,
    require_local_ollama_endpoint,
)

__all__ = ["UntrustedSuggestionEndpointError", "require_local_ollama_endpoint"]
