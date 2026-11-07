"""M3-2: approved predicate mappings suppress repeated suggestions."""

from datetime import UTC, datetime

import pytest

from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    PredicateMapping,
    RelationType,
)
from ontos.ontology.suggestion_validation import (
    has_complete_m2b_review_evidence,
    is_valid_ontology_suggestion,
)
from ontos.ontology.suggestions import OntologySuggestion


def _ontology(*, mapped: bool) -> Ontology:
    mappings = (
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
    )

    return Ontology(
        id="ontos.starter",
        version="0.2",
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
        predicate_mappings=mappings,
    )


def _suggestion(kind: str) -> OntologySuggestion:
    target = "works_at" if kind == "map_to_existing" else "employed_by"

    return OntologySuggestion(
        kind=kind,
        pattern=Pattern(
            subject_type="Person",
            predicate=target,
            object_type="Company",
        ),
        mapped_relation_type=("works_at" if kind == "map_to_existing" else None),
        proposed_relation_type=("employed_by" if kind == "new_relation_type" else None),
        observed_predicate="employed_by",
        extractor_versions=(("extractor", "1"),),
        acl_ref="acl:legal",
        support_count=1,
        supporting_candidate_refs=["opaque-reference"],
        generator_id="test-generator",
        generator_version="1",
        ontology_id="ontos.starter",
        ontology_version="0.2",
        generated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("kind", ["map_to_existing", "new_relation_type"])
def test_unmapped_predicate_remains_eligible(kind: str) -> None:
    ontology = _ontology(mapped=False)
    suggestion = _suggestion(kind)

    assert is_valid_ontology_suggestion(suggestion, ontology)
    assert has_complete_m2b_review_evidence(suggestion, ontology)


@pytest.mark.parametrize("kind", ["map_to_existing", "new_relation_type"])
def test_approved_mapping_suppresses_suggestion(kind: str) -> None:
    ontology = _ontology(mapped=True)
    suggestion = _suggestion(kind)

    assert ontology.resolve_predicate("Person", "employed_by", "Company") == "works_at"

    assert not is_valid_ontology_suggestion(suggestion, ontology)
    assert not has_complete_m2b_review_evidence(suggestion, ontology)
