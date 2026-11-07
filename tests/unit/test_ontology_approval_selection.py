"""M3-2: approval changes must be reconstructed from eligible evidence."""

from __future__ import annotations

import pytest

from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.records import (
    AddPatternChange,
    MapToExistingChange,
    NewRelationTypeChange,
)
from ontos.ontology.approval_selection import (
    ApprovalSuggestionNotFoundError,
    recompute_approval_change,
)
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    PredicateMapping,
    RelationType,
)
from ontos.storage.base import StoredCandidate, candidate_storage_id

KEY = b"approval-selection-test-key"


def _ontology(*, mapped: bool = False) -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.2",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[
            RelationType(label="works_at"),
            RelationType(label="acquired"),
        ],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            )
        ],
        predicate_mappings=(
            [
                PredicateMapping(
                    subject_type="Person",
                    observed_predicate="employed_by",
                    object_type="Company",
                    maps_to="works_at",
                )
            ]
            if mapped
            else []
        ),
    )


def _candidate(
    source: str,
    predicate: str,
    *,
    acl: str | None = "acl:legal",
    version: str = "0.2",
) -> StoredCandidate:
    candidate = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id=f"person-{source}",
            subject_type="Person",
            subject_canonical_name=f"Person {source}",
            predicate=predicate,
            object_id=f"company-{source}",
            object_type="Company",
            object_canonical_name=f"Company {source}",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id=source,
            extractor_id="test-extractor",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version=version,
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )
    return StoredCandidate(
        candidate=candidate,
        stored_id=candidate_storage_id(candidate),
    )


def _recompute(
    change: AddPatternChange | MapToExistingChange | NewRelationTypeChange,
    candidates: list[StoredCandidate],
    *,
    ontology: Ontology | None = None,
):
    return recompute_approval_change(
        authorized_candidates=candidates,
        ontology=ontology or _ontology(),
        requested_change=change,
        acl_ref="acl:legal",
        reference_key=KEY,
    )


def test_add_pattern_is_rebuilt_from_visible_evidence() -> None:
    requested = AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )

    result = _recompute(
        requested,
        [
            _candidate("legal", "acquired"),
            _candidate("finance-secret", "acquired", acl="acl:finance"),
        ],
    )

    assert result.change == requested
    assert result.acl_ref == "acl:legal"
    assert result.support_count == 1
    assert len(result.supporting_candidate_refs) == 1
    assert "finance-secret" not in str(result.supporting_candidate_refs)


def test_existing_relation_mapping_is_supported() -> None:
    requested = MapToExistingChange(
        action="map_to_existing",
        subject_type="Person",
        observed_predicate="employed_by",
        object_type="Company",
        maps_to="works_at",
    )

    result = _recompute(
        requested,
        [_candidate("legal", "employed_by")],
    )

    assert result.change == requested
    assert result.support_count == 1


def test_new_relation_preserves_operator_metadata() -> None:
    requested = NewRelationTypeChange(
        action="new_relation_type",
        subject_type="Person",
        relation_type="mentors",
        object_type="Company",
        description="Mentorship relationship",
        cardinality="many_to_many",
    )

    result = _recompute(
        requested,
        [_candidate("legal", "mentors")],
    )

    assert result.change == requested
    assert result.support_count == 1


def test_hidden_evidence_matches_nonexistent_suggestion() -> None:
    requested = AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )

    messages = []

    for evidence in (
        [],
        [_candidate("finance-secret", "acquired", acl="acl:finance")],
    ):
        with pytest.raises(ApprovalSuggestionNotFoundError) as exc:
            _recompute(requested, evidence)
        messages.append(str(exc.value))

    assert messages == ["No such suggestion", "No such suggestion"]


def test_old_ontology_evidence_cannot_support_approval() -> None:
    requested = MapToExistingChange(
        action="map_to_existing",
        subject_type="Person",
        observed_predicate="employed_by",
        object_type="Company",
        maps_to="works_at",
    )

    with pytest.raises(ApprovalSuggestionNotFoundError, match="No such suggestion"):
        _recompute(
            requested,
            [_candidate("old", "employed_by", version="0.1")],
        )


def test_already_approved_mapping_cannot_be_approved_again() -> None:
    requested = MapToExistingChange(
        action="map_to_existing",
        subject_type="Person",
        observed_predicate="employed_by",
        object_type="Company",
        maps_to="works_at",
    )

    with pytest.raises(ApprovalSuggestionNotFoundError, match="No such suggestion"):
        _recompute(
            requested,
            [_candidate("legal", "employed_by")],
            ontology=_ontology(mapped=True),
        )
