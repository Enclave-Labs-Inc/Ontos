"""M3-1 additive ontology version construction tests."""

from uuid import uuid4

import pytest

from ontos.migration.records import MigrationRecord
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.ontology.versioning import OntologyVersionError, build_next_ontology


def _base() -> Ontology:
    return Ontology(
        id="ontos.test",
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
        ],
    )


def _record(
    changes: list[dict[str, object]],
    **overrides: object,
) -> MigrationRecord:
    payload: dict[str, object] = {
        "schema_version": 1,
        "ontology_id": "ontos.test",
        "from_version": "0.1",
        "to_version": "0.2",
        "migration_kind": "identity",
        "approval_query_id": str(uuid4()),
        "approved_changes": changes,
    }
    payload.update(overrides)
    return MigrationRecord.model_validate(payload)


def _add_pattern() -> dict[str, object]:
    return {
        "action": "add_pattern",
        "subject_type": "Person",
        "predicate": "acquired",
        "object_type": "Company",
    }


def _mapping() -> dict[str, object]:
    return {
        "action": "map_to_existing",
        "subject_type": "Person",
        "observed_predicate": "employed_by",
        "object_type": "Company",
        "maps_to": "works_at",
    }


def _new_relation() -> dict[str, object]:
    return {
        "action": "new_relation_type",
        "subject_type": "Person",
        "relation_type": "mentors",
        "object_type": "Person",
        "description": "Mentorship relationship",
        "cardinality": "many_to_many",
    }


def test_add_pattern_creates_new_version_without_mutating_base() -> None:
    base = _base()
    updated = build_next_ontology(base, _record([_add_pattern()]))

    assert updated.version == "0.2"
    assert updated.id == base.id
    assert updated.is_valid_pattern("Person", "acquired", "Company")
    assert not base.is_valid_pattern("Person", "acquired", "Company")
    assert base.version == "0.1"
    assert updated.check_referential_integrity() == []


def test_mapping_is_scoped_to_approved_type_pair() -> None:
    updated = build_next_ontology(_base(), _record([_mapping()]))

    assert updated.resolve_predicate("Person", "employed_by", "Company") == "works_at"
    assert updated.resolve_predicate("Company", "employed_by", "Person") == "employed_by"
    assert updated.is_valid_pattern("Person", "works_at", "Company")
    assert not updated.is_valid_pattern("Person", "employed_by", "Company")


def test_new_relation_creates_relation_and_pattern() -> None:
    updated = build_next_ontology(_base(), _record([_new_relation()]))

    relation = updated.relation_type("mentors")
    assert relation is not None
    assert relation.description == "Mentorship relationship"
    assert relation.cardinality == "many_to_many"
    assert updated.is_valid_pattern("Person", "mentors", "Person")


def test_multiple_additive_changes_are_applied() -> None:
    base = _base()
    updated = build_next_ontology(
        base,
        _record([_add_pattern(), _mapping(), _new_relation()]),
    )

    assert updated.is_valid_pattern("Person", "acquired", "Company")
    assert updated.is_valid_pattern("Person", "mentors", "Person")
    assert updated.resolve_predicate("Person", "employed_by", "Company") == "works_at"
    assert base.predicate_mappings == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"ontology_id": "another.ontology"},
        {"from_version": "0.0"},
    ],
)
def test_mismatched_base_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(OntologyVersionError):
        build_next_ontology(_base(), _record([_mapping()], **overrides))


def test_duplicate_pattern_is_refused() -> None:
    change = {
        "action": "add_pattern",
        "subject_type": "Person",
        "predicate": "works_at",
        "object_type": "Company",
    }
    with pytest.raises(OntologyVersionError, match="already exists"):
        build_next_ontology(_base(), _record([change]))


def test_duplicate_mapping_is_refused() -> None:
    with pytest.raises(OntologyVersionError, match="already exists"):
        build_next_ontology(_base(), _record([_mapping(), _mapping()]))


def test_existing_relation_cannot_be_redeclared() -> None:
    change = {
        "action": "new_relation_type",
        "subject_type": "Person",
        "relation_type": "works_at",
        "object_type": "Company",
    }
    with pytest.raises(OntologyVersionError, match="already exists"):
        build_next_ontology(_base(), _record([change]))


def test_invalid_target_mapping_is_refused() -> None:
    change = {**_mapping(), "maps_to": "acquired"}
    with pytest.raises(OntologyVersionError, match="invalid resulting ontology"):
        build_next_ontology(_base(), _record([change]))


def test_unknown_entity_type_is_refused() -> None:
    change = {**_add_pattern(), "subject_type": "Unknown"}
    with pytest.raises(OntologyVersionError, match="invalid resulting ontology"):
        build_next_ontology(_base(), _record([change]))


def test_unknown_relation_type_is_refused() -> None:
    change = {**_add_pattern(), "predicate": "nonexistent"}
    with pytest.raises(OntologyVersionError, match="invalid resulting ontology"):
        build_next_ontology(_base(), _record([change]))
