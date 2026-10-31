"""Materialize evidence-backed M2b suggestions for later audited review."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ontos.ontology.loader import Ontology, Pattern
from ontos.ontology.suggestion_support import SupportedSemanticProposal
from ontos.ontology.suggestion_validation import filter_ontology_suggestions
from ontos.ontology.suggestions import OntologySuggestion

_RELATION_LABEL = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class SuggestionMaterializationResult:
    suggestions: tuple[OntologySuggestion, ...]
    invalid_count: int


def materialize_supported_suggestions(
    *,
    supports: Sequence[SupportedSemanticProposal],
    ontology: Ontology,
    generator_id: str,
    generator_version: str,
    generated_at: datetime | None = None,
) -> SuggestionMaterializationResult:
    """Build and revalidate suggestions without accepting model metadata."""
    if not generator_id.strip() or not generator_version.strip():
        raise ValueError("Generator identity and version are required")

    timestamp = generated_at if generated_at is not None else datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")

    entity_types = {item.label for item in ontology.entity_types}
    relation_types = {item.label for item in ontology.relation_types}
    constructed: list[OntologySuggestion] = []
    invalid_count = 0
    seen: set[tuple[str | None, tuple[str, str, str], str, str]] = set()

    for support in supports:
        semantic = support.semantic
        subject, observed_predicate, object_type = semantic.evidence_pattern
        target = semantic.relation_type

        identity = (
            semantic.acl_ref,
            semantic.evidence_pattern,
            semantic.kind,
            target,
        )

        if (
            identity in seen
            or support.support_count < 1
            or not support.supporting_candidate_refs
            or len(set(support.supporting_candidate_refs)) != len(support.supporting_candidate_refs)
            or not support.extractor_versions
            or any(
                not extractor_id or not version
                for extractor_id, version in support.extractor_versions
            )
            or subject not in entity_types
            or object_type not in entity_types
            or len(observed_predicate) > 64
            or not _RELATION_LABEL.fullmatch(observed_predicate)
            or len(target) > 64
            or not _RELATION_LABEL.fullmatch(target)
            or ontology.is_valid_pattern(subject, observed_predicate, object_type)
        ):
            invalid_count += 1
            continue

        if semantic.kind == "map_to_existing":
            valid = target in relation_types and ontology.is_valid_pattern(
                subject, target, object_type
            )
        elif semantic.kind == "new_relation_type":
            valid = target == observed_predicate and target not in relation_types
        else:
            valid = False

        if not valid:
            invalid_count += 1
            continue

        try:
            suggestion = OntologySuggestion(
                kind=semantic.kind,
                pattern=Pattern(
                    subject_type=subject,
                    predicate=target,
                    object_type=object_type,
                ),
                mapped_relation_type=(target if semantic.kind == "map_to_existing" else None),
                proposed_relation_type=(target if semantic.kind == "new_relation_type" else None),
                observed_predicate=observed_predicate,
                extractor_versions=support.extractor_versions,
                acl_ref=semantic.acl_ref,
                support_count=support.support_count,
                supporting_candidate_refs=list(support.supporting_candidate_refs),
                generator_id=generator_id,
                generator_version=generator_version,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                generated_at=timestamp,
            )
        except ValueError:
            invalid_count += 1
            continue

        constructed.append(suggestion)
        seen.add(identity)

    filtered_suggestions, dropped = filter_ontology_suggestions(
        constructed, ontology, require_m2b_evidence=True
    )

    return SuggestionMaterializationResult(
        suggestions=tuple(filtered_suggestions),
        invalid_count=invalid_count + dropped,
    )
