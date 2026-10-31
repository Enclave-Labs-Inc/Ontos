"""Internal, fail-closed M2b LLM suggestion pipeline.

The audited runtime must authorize and VIEW-filter candidates before
calling this function. This module performs no reads, writes, or audits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ontos.ontology.loader import Ontology
from ontos.ontology.suggestion_backend import SuggestionBackend
from ontos.ontology.suggestion_materialization import (
    materialize_supported_suggestions,
)
from ontos.ontology.suggestion_proposals import (
    validate_acl_model_responses,
)
from ontos.ontology.suggestion_requests import (
    request_acl_isolated_suggestions,
)
from ontos.ontology.suggestion_support import resolve_proposal_support
from ontos.ontology.suggestion_trust import (
    require_approved_suggestion_backend,
)
from ontos.ontology.suggestions import OntologySuggestion
from ontos.storage.base import StoredCandidate


@dataclass(frozen=True)
class LLMGenerationResult:
    suggestions: tuple[OntologySuggestion, ...]
    invalid_count: int


async def generate_llm_suggestions(
    *,
    authorized_candidates: Sequence[StoredCandidate],
    ontology: Ontology,
    backend: SuggestionBackend,
    reference_key: bytes,
    generated_at: datetime | None = None,
) -> LLMGenerationResult:
    """Generate only validated, evidence-backed suggestions.

    The production backend gate runs before candidate inspection here.
    Any backend failure aborts the entire batch; no partial result returns.
    """
    approved = require_approved_suggestion_backend(backend)

    if not reference_key:
        raise ValueError("A derived candidate-reference key is required")

    candidates = list(authorized_candidates)

    raw = await request_acl_isolated_suggestions(
        authorized_candidates=candidates,
        ontology=ontology,
        backend=approved,
    )

    validated = validate_acl_model_responses(
        responses=raw,
        ontology=ontology,
    )

    supported = resolve_proposal_support(
        proposals=validated.proposals,
        authorized_candidates=candidates,
        ontology=ontology,
        reference_key=reference_key,
    )

    materialized = materialize_supported_suggestions(
        supports=supported.proposals,
        ontology=ontology,
        generator_id=approved.id,
        generator_version=approved.version,
        generated_at=generated_at,
    )

    return LLMGenerationResult(
        suggestions=materialized.suggestions,
        invalid_count=(
            validated.invalid_count + supported.invalid_count + materialized.invalid_count
        ),
    )
