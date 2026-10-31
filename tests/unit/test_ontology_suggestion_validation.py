"""M2b: validate proposals against the current ontology."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.ontology.suggestion_validation import (
    filter_ontology_suggestions,
    is_valid_ontology_suggestion,
)
from ontos.ontology.suggestions import OntologySuggestion

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def ontology() -> Ontology:
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


def proposal(
    kind: str,
    *,
    subject: str = "Person",
    predicate: str = "works_at",
    object: str = "Company",
    version: str = "0.1",
) -> OntologySuggestion:
    mapped = predicate if kind == "map_to_existing" else None
    proposed = predicate if kind == "new_relation_type" else None

    return OntologySuggestion.model_validate(
        {
            "kind": kind,
            "pattern": Pattern(
                subject_type=subject,
                predicate=predicate,
                object_type=object,
            ),
            "mapped_relation_type": mapped,
            "proposed_relation_type": proposed,
            "acl_ref": "acl:finance",
            "support_count": 1,
            "supporting_candidate_refs": ["protected-reference"],
            "generator_id": "scripted:local",
            "generator_version": "test-v1",
            "ontology_id": "ontos.starter",
            "ontology_version": version,
            "generated_at": NOW,
        }
    )


def test_valid_mapping_to_existing_pattern() -> None:
    assert is_valid_ontology_suggestion(proposal("map_to_existing"), ontology())


def test_mapping_rejects_nonexistent_pattern() -> None:
    assert not is_valid_ontology_suggestion(
        proposal("map_to_existing", predicate="acquired"), ontology()
    )


def test_valid_new_relation_type() -> None:
    assert is_valid_ontology_suggestion(
        proposal(
            "new_relation_type",
            predicate="reports_to",
            object="Person",
        ),
        ontology(),
    )


def test_new_relation_rejects_existing_type() -> None:
    assert not is_valid_ontology_suggestion(proposal("new_relation_type"), ontology())


def test_valid_missing_pattern() -> None:
    assert is_valid_ontology_suggestion(proposal("add_pattern", predicate="acquired"), ontology())


def test_existing_pattern_not_suggested_again() -> None:
    assert not is_valid_ontology_suggestion(proposal("add_pattern"), ontology())


@pytest.mark.parametrize(
    ("subject", "predicate", "object"),
    [
        ("Unknown", "reports_to", "Person"),
        ("Person", "reports_to", "Unknown"),
        ("Person", "Reports To", "Company"),
        ("Person", "reports_to;DROP", "Company"),
        ("Person", "reports_to", "Company<script>"),
    ],
)
def test_invalid_references_or_labels_rejected(
    subject: str,
    predicate: str,
    object: str,
) -> None:
    assert not is_valid_ontology_suggestion(
        proposal(
            "new_relation_type",
            subject=subject,
            predicate=predicate,
            object=object,
        ),
        ontology(),
    )


def test_stale_ontology_rejected() -> None:
    assert not is_valid_ontology_suggestion(proposal("map_to_existing", version="0.0"), ontology())


def test_filter_drops_invalid_and_counts_them() -> None:
    suggestions = [
        proposal("map_to_existing"),
        proposal("map_to_existing", predicate="acquired"),
        proposal("new_relation_type", predicate="reports_to"),
        proposal("new_relation_type", predicate="works_at"),
    ]

    accepted, dropped = filter_ontology_suggestions(suggestions, ontology())

    assert len(accepted) == 2
    assert dropped == 2
    assert [item.kind for item in accepted] == [
        "map_to_existing",
        "new_relation_type",
    ]
