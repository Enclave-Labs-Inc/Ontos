"""Preparation of authorized evidence for M2b ontology suggestions.

Only the audited runtime may supply candidates to this layer.
No storage, authorization, graph writes, or LLM calls happen here.
"""

from __future__ import annotations

from collections import defaultdict

from ontos.ontology.loader import Ontology
from ontos.storage.base import StoredCandidate


def partition_current_suggestion_evidence(
    *,
    authorized_candidates: list[StoredCandidate],
    ontology: Ontology,
) -> dict[str | None, list[StoredCandidate]]:
    """Partition eligible evidence without mixing ACL scopes.

    The caller must have already enforced ontology_reviewer and VIEW.
    Public evidence (None) remains separate from every restricted ACL.
    Only candidates stamped with the current ontology are eligible.
    """
    entity_labels = {item.label for item in ontology.entity_types}

    groups: dict[str | None, list[StoredCandidate]] = defaultdict(list)

    for stored in authorized_candidates:
        candidate = stored.candidate
        triple = candidate.raw_triple
        provenance = candidate.provenance

        if candidate.reason != "pattern_not_in_ontology":
            continue

        if provenance.ontology_id != ontology.id or provenance.ontology_version != ontology.version:
            continue

        if triple.subject_type not in entity_labels or triple.object_type not in entity_labels:
            continue

        if ontology.is_valid_pattern(
            triple.subject_type,
            triple.predicate,
            triple.object_type,
        ):
            continue

        # Unknown predicates remain eligible for new_relation_type.
        groups[candidate.acl_ref].append(stored)

    return dict(groups)
