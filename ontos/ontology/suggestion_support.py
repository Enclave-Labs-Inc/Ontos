"""Server-controlled evidence support for M2b ontology proposals.

Only authorized candidates may enter this layer.
Model-generated references, counts and provenance are never accepted.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ontos.ontology.loader import Ontology
from ontos.ontology.suggestion_generation import (
    partition_current_suggestion_evidence,
)
from ontos.ontology.suggestion_proposals import ValidatedSemanticProposal
from ontos.runtime.candidate_inspection import candidate_audit_reference
from ontos.storage.base import StoredCandidate, candidate_storage_id


@dataclass(frozen=True)
class SupportedSemanticProposal:
    """Validated semantics backed by caller-authorized evidence."""

    semantic: ValidatedSemanticProposal
    support_count: int
    supporting_candidate_refs: tuple[str, ...]
    extractor_versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SupportResolution:
    proposals: tuple[SupportedSemanticProposal, ...]
    invalid_count: int


def resolve_proposal_support(
    *,
    proposals: Sequence[ValidatedSemanticProposal],
    authorized_candidates: list[StoredCandidate],
    ontology: Ontology,
    reference_key: bytes,
) -> SupportResolution:
    """Resolve support without mixing ACLs or trusting model metadata."""
    if not reference_key:
        raise ValueError("A derived candidate-reference key is required")

    groups = partition_current_suggestion_evidence(
        authorized_candidates=authorized_candidates,
        ontology=ontology,
    )

    index: dict[
        tuple[str | None, tuple[str, str, str]],
        list[StoredCandidate],
    ] = defaultdict(list)

    for acl_ref, records in groups.items():
        for stored in records:
            triple = stored.candidate.raw_triple
            pattern = (
                triple.subject_type,
                triple.predicate,
                triple.object_type,
            )
            index[(acl_ref, pattern)].append(stored)

    supported: list[SupportedSemanticProposal] = []
    invalid_count = 0
    seen: set[ValidatedSemanticProposal] = set()

    for proposal in proposals:
        records = index.get(
            (proposal.acl_ref, proposal.evidence_pattern),
            [],
        )

        if not records or proposal in seen:
            invalid_count += 1
            continue

        seen.add(proposal)

        unique_support = {
            (
                stored.candidate.provenance.source_id,
                stored.candidate.raw_triple.subject_id,
                stored.candidate.raw_triple.object_id,
            )
            for stored in records
        }

        references = {
            candidate_audit_reference(
                reference_key,
                candidate_storage_id(stored.candidate),
            )
            for stored in records
        }

        extractors = {
            (
                stored.candidate.provenance.extractor_id,
                stored.candidate.provenance.extractor_version,
            )
            for stored in records
        }

        supported.append(
            SupportedSemanticProposal(
                semantic=proposal,
                support_count=len(unique_support),
                supporting_candidate_refs=tuple(sorted(references)),
                extractor_versions=tuple(sorted(extractors)),
            )
        )

    return SupportResolution(
        proposals=tuple(supported),
        invalid_count=invalid_count,
    )
