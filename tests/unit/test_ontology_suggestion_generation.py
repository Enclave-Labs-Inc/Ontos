"""ACL partitioning and ontology freshness tests for M2b."""

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
from ontos.ontology.suggestion_generation import (
    partition_current_suggestion_evidence,
)
from ontos.storage.base import StoredCandidate


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
    predicate: str = "owns",
    subject_type: str = "Person",
    ontology_id: str = "test.ontology",
    ontology_version: str = "1.0",
    reason: str = "pattern_not_in_ontology",
) -> StoredCandidate:
    candidate = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id=f"person-{source}",
            subject_type=subject_type,
            subject_canonical_name="Example Person",
            predicate=predicate,
            object_id=f"company-{source}",
            object_type="Company",
            object_canonical_name="Example Company",
            llm_confidence=0.9,
        ),
        reason=reason,
        provenance=CandidateProvenance(
            source_id=source,
            extractor_id="test-extractor",
            extractor_version="1",
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )
    return StoredCandidate(
        candidate=candidate,
        stored_id=f"stored-{source}",
    )


def test_restricted_acls_never_merge() -> None:
    first = _candidate("a", "finance")
    second = _candidate("b", "legal")
    third = _candidate("c", "finance")

    groups = partition_current_suggestion_evidence(
        authorized_candidates=[first, second, third],
        ontology=_ontology(),
    )

    assert groups == {
        "finance": [first, third],
        "legal": [second],
    }


def test_public_remains_separate_from_restricted() -> None:
    public = _candidate("public", None)
    restricted = _candidate("private", "finance")

    groups = partition_current_suggestion_evidence(
        authorized_candidates=[public, restricted],
        ontology=_ontology(),
    )

    assert groups[None] == [public]
    assert groups["finance"] == [restricted]


def test_stale_ontology_id_and_version_are_excluded() -> None:
    wrong_id = _candidate("a", "finance", ontology_id="other")
    wrong_version = _candidate("b", "finance", ontology_version="0.9")
    current = _candidate("c", "finance")

    groups = partition_current_suggestion_evidence(
        authorized_candidates=[wrong_id, wrong_version, current],
        ontology=_ontology(),
    )

    assert groups == {"finance": [current]}


def test_unknown_entity_types_are_excluded() -> None:
    invalid = _candidate("a", "finance", subject_type="Unknown")
    groups = partition_current_suggestion_evidence(
        authorized_candidates=[invalid],
        ontology=_ontology(),
    )
    assert groups == {}


def test_already_valid_pattern_is_excluded() -> None:
    valid = _candidate("a", "finance", predicate="works_at")
    groups = partition_current_suggestion_evidence(
        authorized_candidates=[valid],
        ontology=_ontology(),
    )
    assert groups == {}


def test_unknown_predicate_remains_eligible() -> None:
    unknown = _candidate("a", "finance", predicate="invented_relation")
    groups = partition_current_suggestion_evidence(
        authorized_candidates=[unknown],
        ontology=_ontology(),
    )
    assert groups == {"finance": [unknown]}


def test_low_confidence_rejections_are_excluded() -> None:
    rejected = _candidate("a", "finance", reason="below_min_confidence")
    groups = partition_current_suggestion_evidence(
        authorized_candidates=[rejected],
        ontology=_ontology(),
    )
    assert groups == {}
