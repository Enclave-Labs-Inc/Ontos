"""Materialize evidence-backed M2b suggestions for later audited review."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ontos.ontology.labels import is_relation_label, is_safe_label
from ontos.ontology.loader import Ontology, Pattern
from ontos.ontology.suggestion_support import SupportedSemanticProposal
from ontos.ontology.suggestions import OntologySuggestion


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
            # Observed predicates are model-facing: lower_snake_case only.
            # Targets may be an existing ontology label such as `WORKS_AT`;
            # membership below is what makes a mapping target safe.
            or not is_relation_label(observed_predicate)
            or not is_safe_label(target)
            or ontology.is_valid_pattern(subject, observed_predicate, object_type)
        ):
            invalid_count += 1
            continue

        if semantic.kind == "map_to_existing":
            valid = target in relation_types and ontology.is_valid_pattern(
                subject, target, object_type
            )
        elif semantic.kind == "new_relation_type":
            valid = (
                target == observed_predicate
                and target not in relation_types
                and is_relation_label(target)
            )
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

    # The ontology-consistency filter runs once, over M2a and M2b output
    # together, in the audited service (#90).
    return SuggestionMaterializationResult(
        suggestions=tuple(constructed),
        invalid_count=invalid_count,
    )
