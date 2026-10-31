"""M2b typed suggestion model regression tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ontos.ontology.loader import Pattern
from ontos.ontology.suggestions import OntologySuggestion

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _proposal(kind: str, **changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": kind,
        "pattern": Pattern(
            subject_type="Person",
            predicate="works_at",
            object_type="Company",
        ),
        "acl_ref": "acl:finance",
        "support_count": 1,
        "supporting_candidate_refs": ["protected-test-reference"],
        "generator_id": "scripted:local",
        "generator_version": "test-v1",
        "ontology_id": "ontos.starter",
        "ontology_version": "0.1",
        "generated_at": NOW,
    }
    data.update(changes)
    return data


def test_add_pattern_still_valid() -> None:
    result = OntologySuggestion.model_validate(_proposal("add_pattern"))
    assert result.kind == "add_pattern"


def test_map_to_existing_valid_shape() -> None:
    result = OntologySuggestion.model_validate(
        _proposal("map_to_existing", mapped_relation_type="works_at")
    )
    assert result.mapped_relation_type == "works_at"
    assert result.proposed_relation_type is None


def test_new_relation_type_valid_shape() -> None:
    result = OntologySuggestion.model_validate(
        _proposal(
            "new_relation_type",
            pattern=Pattern(
                subject_type="Person",
                predicate="mentors",
                object_type="Person",
            ),
            proposed_relation_type="mentors",
        )
    )
    assert result.proposed_relation_type == "mentors"


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("map_to_existing", {}),
        ("new_relation_type", {}),
        ("add_pattern", {"mapped_relation_type": "works_at"}),
        ("add_pattern", {"proposed_relation_type": "mentors"}),
        ("map_to_existing", {"mapped_relation_type": "mentors"}),
        (
            "map_to_existing",
            {"mapped_relation_type": "works_at", "proposed_relation_type": "other"},
        ),
        ("new_relation_type", {"proposed_relation_type": "mentors"}),
        (
            "new_relation_type",
            {"proposed_relation_type": "works_at", "mapped_relation_type": "works_at"},
        ),
        ("map_to_existing", {"pattern": None, "mapped_relation_type": "works_at"}),
    ],
)
def test_invalid_kind_field_combinations_rejected(kind: str, changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OntologySuggestion.model_validate(_proposal(kind, **changes))
