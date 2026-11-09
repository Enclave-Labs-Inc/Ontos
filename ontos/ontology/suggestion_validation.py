"""Validate untrusted ontology proposals against the active ontology.

This module does not authorize candidate evidence or call an LLM.
It only validates already-constructed, typed suggestions.
"""

from __future__ import annotations

from ontos.ontology.labels import RELATION_LABEL, is_relation_label, is_safe_label
from ontos.ontology.labels import SAFE_LABEL as ENTITY_LABEL
from ontos.ontology.loader import Ontology
from ontos.ontology.suggestions import OntologySuggestion

__all__ = ["ENTITY_LABEL", "RELATION_LABEL", "is_valid_ontology_suggestion"]


def is_valid_ontology_suggestion(
    suggestion: OntologySuggestion,
    ontology: Ontology,
) -> bool:
    """Reject stale, malformed or ontology-inconsistent proposals."""
    if suggestion.ontology_id != ontology.id or suggestion.ontology_version != ontology.version:
        return False

    pattern = suggestion.pattern
    if pattern is None:
        return False

    # An already-approved predicate mapping must not be proposed again.
    if suggestion.kind != "add_pattern" and suggestion.observed_predicate is not None:
        observed = suggestion.observed_predicate
        resolved = ontology.resolve_predicate(pattern.subject_type, observed, pattern.object_type)
        if resolved != observed:
            return False

    if not (
        is_safe_label(pattern.subject_type)
        and is_safe_label(pattern.object_type)
        and is_safe_label(pattern.predicate)
    ):
        return False

    if (
        ontology.entity_type(pattern.subject_type) is None
        or ontology.entity_type(pattern.object_type) is None
    ):
        return False

    exists = ontology.relation_type(pattern.predicate) is not None
    allowed = ontology.is_valid_pattern(
        pattern.subject_type,
        pattern.predicate,
        pattern.object_type,
    )

    if suggestion.kind == "add_pattern":
        return exists and not allowed

    if suggestion.kind == "map_to_existing":
        return exists and allowed and suggestion.mapped_relation_type == pattern.predicate

    if suggestion.kind == "new_relation_type":
        return (
            not exists
            and not allowed
            and suggestion.proposed_relation_type == pattern.predicate
            and is_relation_label(pattern.predicate)
        )

    return False


def filter_ontology_suggestions(
    suggestions: list[OntologySuggestion],
    ontology: Ontology,
    *,
    require_m2b_evidence: bool = False,
) -> tuple[list[OntologySuggestion], int]:
    """Drop invalid proposals and return their count."""
    accepted: list[OntologySuggestion] = []
    dropped = 0

    for suggestion in suggestions:
        if is_valid_ontology_suggestion(suggestion, ontology) and (
            not require_m2b_evidence or has_complete_m2b_review_evidence(suggestion, ontology)
        ):
            accepted.append(suggestion)
        else:
            dropped += 1

    return accepted, dropped


def has_complete_m2b_review_evidence(
    suggestion: OntologySuggestion,
    ontology: Ontology,
) -> bool:
    """Require coherent evidence metadata for reviewer-facing M2b output."""
    if suggestion.kind == "add_pattern":
        return True

    pattern = suggestion.pattern
    observed = suggestion.observed_predicate

    if (
        pattern is None
        or observed is None
        or len(observed) > 64
        or RELATION_LABEL.fullmatch(observed) is None
    ):
        return False

    extractors = suggestion.extractor_versions
    if not extractors or len(set(extractors)) != len(extractors):
        return False

    if any(
        not extractor_id.strip()
        or not version.strip()
        or len(extractor_id) > 128
        or len(version) > 128
        for extractor_id, version in extractors
    ):
        return False

    # Rejected evidence must not already be an allowed ontology pattern.
    resolved = ontology.resolve_predicate(pattern.subject_type, observed, pattern.object_type)
    if resolved != observed or ontology.is_valid_pattern(
        pattern.subject_type, resolved, pattern.object_type
    ):
        return False

    if suggestion.kind == "map_to_existing":
        return observed != pattern.predicate

    if suggestion.kind == "new_relation_type":
        return observed == pattern.predicate

    return False
