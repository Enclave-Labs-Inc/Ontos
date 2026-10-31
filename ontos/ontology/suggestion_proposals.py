"""Validate untrusted M2b model proposals before constructing suggestions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from ontos.ontology.loader import Ontology
from ontos.ontology.suggestion_requests import ACLScopedRawResponse

_VALID_RELATION = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_PROPOSAL_FIELDS = frozenset({"candidate_id", "kind", "relation_type"})


@dataclass(frozen=True)
class ValidatedSemanticProposal:
    """Semantics verified against current ontology and authorized evidence."""

    acl_ref: str | None
    evidence_pattern: tuple[str, str, str]
    kind: Literal["map_to_existing", "new_relation_type"]
    relation_type: str


@dataclass(frozen=True)
class ProposalValidationResult:
    proposals: tuple[ValidatedSemanticProposal, ...]
    invalid_count: int


def validate_acl_model_responses(
    *,
    responses: Sequence[ACLScopedRawResponse],
    ontology: Ontology,
) -> ProposalValidationResult:
    """Drop invalid output; never adopt model-supplied trust metadata."""
    accepted: list[ValidatedSemanticProposal] = []
    invalid_count = 0

    entity_labels = {entity.label for entity in ontology.entity_types}
    relation_labels = {relation.label for relation in ontology.relation_types}

    for response in responses:
        payload = response.model_response

        if set(payload) != {"proposals"} or not isinstance(payload.get("proposals"), list):
            invalid_count += 1
            continue

        items = payload["proposals"]

        # One proposal per evidence pattern. Excess output is refused.
        if len(items) > len(response.evidence_patterns):
            invalid_count += len(items)
            continue

        seen_ids: set[int] = set()

        for item in items:
            if not isinstance(item, dict) or set(item) != _PROPOSAL_FIELDS:
                invalid_count += 1
                continue

            candidate_id = item["candidate_id"]
            kind = item["kind"]
            relation_type = item["relation_type"]

            if (
                type(candidate_id) is not int
                or candidate_id < 0
                or candidate_id >= len(response.evidence_patterns)
                or candidate_id in seen_ids
                or not isinstance(kind, str)
                or not isinstance(relation_type, str)
                or len(relation_type) > 64
                or not _VALID_RELATION.fullmatch(relation_type)
            ):
                invalid_count += 1
                continue

            subject, predicate, object_type = response.evidence_patterns[candidate_id]

            if (
                subject not in entity_labels
                or object_type not in entity_labels
                or ontology.is_valid_pattern(subject, predicate, object_type)
            ):
                invalid_count += 1
                continue

            if kind == "map_to_existing":
                valid = relation_type in relation_labels and ontology.is_valid_pattern(
                    subject, relation_type, object_type
                )
            elif kind == "new_relation_type":
                # The new name must be grounded in observed evidence.
                # The model may classify, but not invent an unrelated name.
                valid = relation_type not in relation_labels and relation_type == predicate
            else:
                valid = False

            if not valid:
                invalid_count += 1
                continue

            seen_ids.add(candidate_id)
            accepted.append(
                ValidatedSemanticProposal(
                    acl_ref=response.acl_ref,
                    evidence_pattern=(subject, predicate, object_type),
                    kind=cast(Literal["map_to_existing", "new_relation_type"], kind),
                    relation_type=relation_type,
                )
            )

    return ProposalValidationResult(
        proposals=tuple(accepted),
        invalid_count=invalid_count,
    )
