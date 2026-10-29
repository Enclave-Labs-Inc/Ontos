"""Unit tests for deterministic ontology suggestions (issue #66, M2a)."""

from __future__ import annotations

from datetime import UTC, datetime

from ontos.extraction import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
)
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.ontology.suggestions import suggest_missing_patterns
from ontos.runtime.candidate_inspection import candidate_audit_reference
from ontos.storage.base import StoredCandidate, candidate_storage_id

REFERENCE_KEY = b"unit-test-derived-reference-key"
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _ontology() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.1",
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
            ),
            Pattern(
                subject_type="Company",
                predicate="acquired",
                object_type="Company",
            ),
        ],
    )


def _candidate(
    *,
    source_id: str = "document-1",
    acl_ref: str | None = "acl:finance",
    subject_type: str = "Person",
    predicate: str = "acquired",
    object_type: str = "Company",
    reason: str = "pattern_not_in_ontology",
    ontology_version: str = "0.1",
) -> StoredCandidate:
    candidate = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="entity-1",
            subject_type=subject_type,
            subject_canonical_name="Entity One",
            predicate=predicate,
            object_id="entity-2",
            object_type=object_type,
            object_canonical_name="Entity Two",
            llm_confidence=0.9,
        ),
        reason=reason,
        provenance=CandidateProvenance(
            source_id=source_id,
            extractor_id="test-extractor",
            extractor_version="1.0",
            ontology_id="ontos.starter",
            ontology_version=ontology_version,
            confidence_score=0.9,
        ),
        acl_ref=acl_ref,
    )
    return StoredCandidate(
        candidate=candidate,
        stored_id=candidate_storage_id(candidate),
    )


def _suggest(candidates: list[StoredCandidate]):
    return suggest_missing_patterns(
        candidates=candidates,
        ontology=_ontology(),
        reference_key=REFERENCE_KEY,
        generated_at=NOW,
    )


def test_suggests_missing_pattern() -> None:
    stored = _candidate()
    suggestions = _suggest([stored])

    assert len(suggestions) == 1
    suggestion = suggestions[0]

    assert suggestion.kind == "add_pattern"
    assert suggestion.pattern == Pattern(
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )
    assert suggestion.acl_ref == "acl:finance"
    assert suggestion.support_count == 1
    assert suggestion.ontology_id == "ontos.starter"
    assert suggestion.ontology_version == "0.1"
    assert suggestion.generated_at == NOW
    assert suggestion.generator_id == "ontos.ontology.deterministic"

    expected_ref = candidate_audit_reference(
        REFERENCE_KEY,
        stored.stored_id,
    )
    assert suggestion.supporting_candidate_refs == [expected_ref]
    assert stored.stored_id not in suggestion.model_dump_json()


def test_aggregates_support_within_same_acl() -> None:
    candidates = [
        _candidate(source_id="doc-a"),
        _candidate(source_id="doc-b"),
        _candidate(source_id="doc-c"),
    ]

    suggestions = _suggest(candidates)

    assert len(suggestions) == 1
    assert suggestions[0].support_count == 3
    assert len(suggestions[0].supporting_candidate_refs) == 3


def test_never_combines_different_acls() -> None:
    candidates = [
        _candidate(source_id="finance-1", acl_ref="acl:finance"),
        _candidate(source_id="finance-2", acl_ref="acl:finance"),
        _candidate(source_id="hr-1", acl_ref="acl:hr"),
    ]

    suggestions = _suggest(candidates)

    assert len(suggestions) == 2
    by_acl = {s.acl_ref: s for s in suggestions}

    assert by_acl["acl:finance"].support_count == 2
    assert by_acl["acl:hr"].support_count == 1
    assert not (
        set(by_acl["acl:finance"].supporting_candidate_refs)
        & set(by_acl["acl:hr"].supporting_candidate_refs)
    )


def test_excludes_stale_ontology_version() -> None:
    assert _suggest([_candidate(ontology_version="0.0")]) == []


def test_does_not_suggest_existing_pattern() -> None:
    assert _suggest([_candidate(predicate="works_at")]) == []


def test_does_not_suggest_undeclared_relationship() -> None:
    assert _suggest([_candidate(predicate="mentors")]) == []


def test_does_not_suggest_undeclared_entity_type() -> None:
    assert _suggest([_candidate(subject_type="UnknownType")]) == []


def test_does_not_use_low_confidence_rejections() -> None:
    assert _suggest([_candidate(reason="below_min_confidence")]) == []


def test_empty_candidates_yield_no_suggestions() -> None:
    assert _suggest([]) == []


def test_missing_reference_key_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="reference key"):
        suggest_missing_patterns(
            candidates=[_candidate()],
            ontology=_ontology(),
            reference_key=b"",
        )


def test_reextracting_same_relationship_does_not_inflate_support() -> None:
    original = _candidate(source_id="document-1")
    updated_candidate = original.candidate.model_copy(
        update={
            "provenance": original.candidate.provenance.model_copy(
                update={"extractor_version": "2.0"}
            )
        }
    )
    updated = StoredCandidate(
        candidate=updated_candidate,
        stored_id=candidate_storage_id(updated_candidate),
    )

    suggestions = _suggest([original, updated])

    assert len(suggestions) == 1
    assert suggestions[0].support_count == 1
    assert len(suggestions[0].supporting_candidate_refs) == 2


def _suggestion_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "kind": "add_pattern",
        "pattern": Pattern(subject_type="Person", predicate="works_at", object_type="Company"),
        "acl_ref": "acl:finance",
        "support_count": 1,
        "supporting_candidate_refs": ["ref"],
        "generator_id": "test",
        "generator_version": "1",
        "ontology_id": "o",
        "ontology_version": "1",
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return fields


def test_add_pattern_suggestion_requires_pattern() -> None:
    """#85: kind and fields must agree."""
    import pytest
    from pydantic import ValidationError

    from ontos.ontology.suggestions import OntologySuggestion

    OntologySuggestion(**_suggestion_fields())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="require a pattern"):
        OntologySuggestion(**_suggestion_fields(pattern=None))  # type: ignore[arg-type]


def test_m2b_suggestion_kinds_are_rejected_until_specified() -> None:
    import pytest
    from pydantic import ValidationError

    from ontos.ontology.suggestions import OntologySuggestion

    for kind in ("map_to_existing", "new_relation_type"):
        with pytest.raises(ValidationError, match="not supported yet"):
            OntologySuggestion(**_suggestion_fields(kind=kind))  # type: ignore[arg-type]
