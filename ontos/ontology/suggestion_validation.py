"""Validate untrusted ontology proposals against the active ontology.

This module does not authorize candidate evidence or call an LLM.
It only validates already-constructed, typed suggestions.
"""

from __future__ import annotations

import re

from ontos.ontology.loader import Ontology
from ontos.ontology.suggestions import OntologySuggestion

RELATION_LABEL = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
ENTITY_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


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

    if not (
        ENTITY_LABEL.fullmatch(pattern.subject_type)
        and ENTITY_LABEL.fullmatch(pattern.object_type)
        and RELATION_LABEL.fullmatch(pattern.predicate)
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
        return not exists and not allowed and suggestion.proposed_relation_type == pattern.predicate

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
    if ontology.is_valid_pattern(pattern.subject_type, observed, pattern.object_type):
        return False

    if suggestion.kind == "map_to_existing":
        return observed != pattern.predicate

    if suggestion.kind == "new_relation_type":
        return observed == pattern.predicate

    return False
