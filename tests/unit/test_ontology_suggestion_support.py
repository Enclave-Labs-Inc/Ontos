"""Scripted tests for server-controlled M2b evidence support."""

import pytest

from ontos.extraction import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
)
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    RelationType,
)
from ontos.ontology.suggestion_proposals import ValidatedSemanticProposal
from ontos.ontology.suggestion_support import resolve_proposal_support
from ontos.runtime.candidate_inspection import candidate_audit_reference
from ontos.storage.base import StoredCandidate, candidate_storage_id

KEY = b"test-derived-reference-key"


def _ontology() -> Ontology:
    return Ontology(
        id="test.ontology",
        version="1.0",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[RelationType(label="works_at")],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            )
        ],
    )


def _candidate(
    source: str,
    acl: str | None,
    *,
    extractor_version: str = "1",
    ontology_version: str = "1.0",
    reason: str = "pattern_not_in_ontology",
) -> StoredCandidate:
    return StoredCandidate(
        stored_id=f"UNTRUSTED-STORAGE-{source}",
        candidate=RejectedCandidate(
            raw_triple=CandidateTriple(
                subject_id=f"person-{source}",
                subject_type="Person",
                subject_canonical_name="Example Person",
                predicate="manages",
                object_id=f"company-{source}",
                object_type="Company",
                object_canonical_name="Example Company",
                llm_confidence=0.9,
            ),
            reason=reason,
            provenance=CandidateProvenance(
                source_id=source,
                extractor_id="extractor",
                extractor_version=extractor_version,
                ontology_id="test.ontology",
                ontology_version=ontology_version,
                confidence_score=0.9,
            ),
            acl_ref=acl,
        ),
    )


def _proposal(acl: str | None) -> ValidatedSemanticProposal:
    return ValidatedSemanticProposal(
        acl_ref=acl,
        evidence_pattern=("Person", "manages", "Company"),
        kind="map_to_existing",
        relation_type="works_at",
    )


def _reference(stored: StoredCandidate) -> str:
    return candidate_audit_reference(KEY, candidate_storage_id(stored.candidate))


def test_repeated_extraction_deduplicates_count_not_refs() -> None:
    first = _candidate("source1", "finance", extractor_version="1")
    second = _candidate("source1", "finance", extractor_version="2")

    result = resolve_proposal_support(
        proposals=[_proposal("finance")],
        authorized_candidates=[first, second],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert result.invalid_count == 0
    assert len(result.proposals) == 1

    support = result.proposals[0]
    assert support.support_count == 1
    assert support.supporting_candidate_refs == tuple(
        sorted({_reference(first), _reference(second)})
    )
    assert support.extractor_versions == (
        ("extractor", "1"),
        ("extractor", "2"),
    )
    assert all("UNTRUSTED-STORAGE" not in ref for ref in support.supporting_candidate_refs)


def test_support_is_never_shared_across_acls() -> None:
    finance = _candidate("finance-doc", "finance")
    legal = _candidate("legal-doc", "legal")

    result = resolve_proposal_support(
        proposals=[_proposal("finance")],
        authorized_candidates=[finance, legal],
        ontology=_ontology(),
        reference_key=KEY,
    )

    support = result.proposals[0]
    assert support.support_count == 1
    assert support.supporting_candidate_refs == (_reference(finance),)
    assert _reference(legal) not in support.supporting_candidate_refs


def test_stale_evidence_does_not_contribute() -> None:
    current = _candidate("current", "finance")
    stale = _candidate("stale", "finance", ontology_version="0.9")

    result = resolve_proposal_support(
        proposals=[_proposal("finance")],
        authorized_candidates=[current, stale],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert result.proposals[0].supporting_candidate_refs == (_reference(current),)
    assert result.proposals[0].support_count == 1


def test_missing_acl_support_drops_proposal() -> None:
    result = resolve_proposal_support(
        proposals=[_proposal("legal")],
        authorized_candidates=[_candidate("x", "finance")],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert result.proposals == ()
    assert result.invalid_count == 1


def test_distinct_sources_count_separately() -> None:
    result = resolve_proposal_support(
        proposals=[_proposal(None)],
        authorized_candidates=[
            _candidate("source1", None),
            _candidate("source2", None),
        ],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert result.proposals[0].support_count == 2
    assert len(result.proposals[0].supporting_candidate_refs) == 2


def test_duplicate_proposals_are_not_returned_twice() -> None:
    proposal = _proposal("finance")

    result = resolve_proposal_support(
        proposals=[proposal, proposal],
        authorized_candidates=[_candidate("x", "finance")],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert len(result.proposals) == 1
    assert result.invalid_count == 1


def test_invalid_reason_does_not_contribute() -> None:
    result = resolve_proposal_support(
        proposals=[_proposal("finance")],
        authorized_candidates=[_candidate("x", "finance", reason="below_min_confidence")],
        ontology=_ontology(),
        reference_key=KEY,
    )

    assert result.proposals == ()
    assert result.invalid_count == 1


def test_empty_reference_key_fails_closed() -> None:
    with pytest.raises(ValueError, match="derived"):
        resolve_proposal_support(
            proposals=[_proposal("finance")],
            authorized_candidates=[_candidate("x", "finance")],
            ontology=_ontology(),
            reference_key=b"",
        )
