"""M1.c — ontology loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.ontology import (
    Cardinality,
    Ontology,
    OntologyLoadError,
    Pattern,
    PropertyDef,
    load_ontology,
)

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


def test_starter_ontology_loads(tmp_path: Path) -> None:
    ont = load_ontology(STARTER_PATH)
    assert isinstance(ont, Ontology)
    assert ont.entity_type("Person") is not None
    assert ont.entity_type("Company") is not None
    assert ont.relation_type("works_at") is not None
    assert ont.relation_type("acquired") is not None
    assert ont.is_valid_pattern("Person", "works_at", "Company")
    assert not ont.is_valid_pattern("Company", "works_at", "Person")


def test_missing_file_raises_load_error(tmp_path: Path) -> None:
    with pytest.raises(OntologyLoadError) as exc:
        load_ontology(tmp_path / "nope.yaml")
    assert "file not found" in str(exc.value)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just: a list\n")
    with pytest.raises(OntologyLoadError) as exc:
        load_ontology(bad)
    assert "top-level YAML value must be a mapping" in str(exc.value)


def test_ontology_needs_entity_types(tmp_path: Path) -> None:
    bad = tmp_path / "no_entities.yaml"
    bad.write_text("entity_types: []\nrelation_types:\n  - label: r\npatterns: []\n")
    with pytest.raises(OntologyLoadError):
        load_ontology(bad)


def test_ontology_needs_relation_types(tmp_path: Path) -> None:
    bad = tmp_path / "no_rels.yaml"
    bad.write_text("entity_types:\n  - label: E\nrelation_types: []\npatterns: []\n")
    with pytest.raises(OntologyLoadError):
        load_ontology(bad)


def test_referential_integrity_catches_dangling_pattern(tmp_path: Path) -> None:
    bad = tmp_path / "dangling.yaml"
    bad.write_text(
        "entity_types:\n"
        "  - label: Person\n"
        "relation_types:\n"
        "  - label: knows\n"
        "patterns:\n"
        "  - subject_type: Person\n"
        "    predicate: knows\n"
        "    object_type: Ghost\n"  # not declared
    )
    with pytest.raises(OntologyLoadError) as exc:
        load_ontology(bad)
    assert "Ghost" in str(exc.value)


def test_referential_integrity_catches_undeclared_predicate(tmp_path: Path) -> None:
    bad = tmp_path / "bad_pred.yaml"
    bad.write_text(
        "entity_types:\n"
        "  - label: Person\n"
        "relation_types:\n"
        "  - label: knows\n"
        "patterns:\n"
        "  - subject_type: Person\n"
        "    predicate: haunts\n"  # not declared
        "    object_type: Person\n"
    )
    with pytest.raises(OntologyLoadError) as exc:
        load_ontology(bad)
    assert "haunts" in str(exc.value)


def test_reserved_property_names_rejected() -> None:
    import pydantic

    for reserved in ("id", "fact_id", "predicate"):
        with pytest.raises(pydantic.ValidationError):
            PropertyDef(name=reserved, type="STRING")


def test_invalid_property_type_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PropertyDef(name="foo", type="BLOB")  # type: ignore[arg-type]


def test_invalid_cardinality_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad_card.yaml"
    bad.write_text(
        "entity_types:\n"
        "  - label: E\n"
        "relation_types:\n"
        "  - label: r\n"
        "    cardinality: exclusive_or\n"
        "patterns: []\n"
    )
    with pytest.raises(OntologyLoadError):
        load_ontology(bad)


def test_ontology_is_frozen() -> None:
    import pydantic

    ont = load_ontology(STARTER_PATH)
    try:
        ont.version = "9.9.9"  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("Ontology must be frozen")


def test_pattern_helpers_return_none_for_unknown() -> None:
    ont = load_ontology(STARTER_PATH)
    assert ont.entity_type("Unknown") is None
    assert ont.relation_type("nonexistent") is None
    assert not ont.is_valid_pattern("Unknown", "works_at", "Company")


def test_linkml_extension_hits_not_implemented(tmp_path: Path) -> None:
    stub = tmp_path / "example.linkml.yaml"
    stub.write_text("id: https://example.org/schema\n")
    with pytest.raises(NotImplementedError) as exc:
        load_ontology(stub)
    assert "M2" in str(exc.value)


def test_pattern_and_property_models_are_frozen() -> None:
    import pydantic

    prop = PropertyDef(name="role", type="STRING")
    try:
        prop.type = "INTEGER"  # type: ignore[misc]
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("PropertyDef must be frozen")

    pat = Pattern(subject_type="Person", predicate="works_at", object_type="Company")
    try:
        pat.predicate = "acquired"  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("Pattern must be frozen")


@pytest.mark.parametrize(
    "value",
    ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
)
def test_all_cardinalities_valid(value: Cardinality) -> None:
    # Belt-and-braces — if we add a new cardinality, this parametrization forces us
    # to consciously extend the test.
    from ontos.ontology import RelationType

    rt = RelationType(label="r", cardinality=value)
    assert rt.cardinality == value


def test_starter_ontology_cardinality_parses() -> None:
    ont = load_ontology(STARTER_PATH)
    works_at = ont.relation_type("works_at")
    assert works_at is not None
    assert works_at.cardinality == "many_to_one"
    acquired = ont.relation_type("acquired")
    assert acquired is not None
    assert acquired.cardinality == "many_to_many"
