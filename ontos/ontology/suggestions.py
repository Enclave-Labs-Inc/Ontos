"""Deterministic, untrusted ontology suggestions (M2a).

Input must already be authorization- and VIEW-filtered.
This module does not access storage, call models, or modify an ontology.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ontos.ontology.loader import Ontology, Pattern
from ontos.runtime.candidate_inspection import candidate_audit_reference
from ontos.storage.base import StoredCandidate


class OntologySuggestion(BaseModel):
    """A proposal for human review, never an approved ontology change."""

    kind: Literal["add_pattern", "map_to_existing", "new_relation_type"]
    pattern: Pattern | None = None
    acl_ref: str | None
    support_count: int = Field(ge=1)
    supporting_candidate_refs: list[str] = Field(min_length=1)
    generator_id: str
    generator_version: str
    ontology_id: str
    ontology_version: str
    generated_at: datetime

    model_config = ConfigDict(frozen=True)


def suggest_missing_patterns(
    *,
    candidates: list[StoredCandidate],
    ontology: Ontology,
    reference_key: bytes,
    generated_at: datetime | None = None,
) -> list[OntologySuggestion]:
    """Suggest missing patterns from an already authorized candidate set.

    Never aggregate across ACLs. Never return raw candidate storage IDs.
    """
    if not reference_key:
        raise ValueError("A derived candidate-reference key is required")

    entity_types = {item.label for item in ontology.entity_types}
    relation_types = {item.label for item in ontology.relation_types}

    groups: dict[
        tuple[str | None, str, str, str],
        set[str],
    ] = defaultdict(set)

    for stored in candidates:
        candidate = stored.candidate
        triple = candidate.raw_triple
        provenance = candidate.provenance

        if candidate.reason != "pattern_not_in_ontology":
            continue

        if provenance.ontology_id != ontology.id or provenance.ontology_version != ontology.version:
            continue

        if (
            triple.subject_type not in entity_types
            or triple.object_type not in entity_types
            or triple.predicate not in relation_types
        ):
            continue

        if ontology.is_valid_pattern(
            triple.subject_type,
            triple.predicate,
            triple.object_type,
        ):
            continue

        key = (
            candidate.acl_ref,
            triple.subject_type,
            triple.predicate,
            triple.object_type,
        )

        groups[key].add(candidate_audit_reference(reference_key, stored.stored_id))

    timestamp = generated_at or datetime.now(UTC)
    suggestions: list[OntologySuggestion] = []

    for key in sorted(
        groups, key=lambda value: tuple("" if part is None else part for part in value)
    ):
        acl_ref, subject_type, predicate, object_type = key
        references = sorted(groups[key])

        suggestions.append(
            OntologySuggestion(
                kind="add_pattern",
                pattern=Pattern(
                    subject_type=subject_type,
                    predicate=predicate,
                    object_type=object_type,
                ),
                acl_ref=acl_ref,
                support_count=len(references),
                supporting_candidate_refs=references,
                generator_id="ontos.ontology.deterministic",
                generator_version="m2a-v1",
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                generated_at=timestamp,
            )
        )

    return suggestions
