"""M3-1 predicate mapping schema and YAML regression tests."""

from pathlib import Path

import pytest
import yaml

from ontos.ontology import OntologyLoadError, PredicateMapping, load_ontology

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _mapping(**overrides: str) -> dict[str, str]:
    result = {
        "subject_type": "Person",
        "observed_predicate": "employed_by",
        "object_type": "Company",
        "maps_to": "works_at",
    }
    result.update(overrides)
    return result


def _load_with_mappings(tmp_path: Path, mappings: list[dict[str, str]]):
    raw = yaml.safe_load(STARTER.read_text())
    raw["predicate_mappings"] = mappings

    path = tmp_path / "mapped.yaml"
    path.write_text(yaml.safe_dump(raw))

    return load_ontology(path)


def test_legacy_ontology_remains_compatible() -> None:
    ontology = load_ontology(STARTER)

    assert ontology.predicate_mappings == []
    assert ontology.resolve_predicate("Person", "employed_by", "Company") == "employed_by"


def test_mapping_is_scoped_to_exact_type_pair(tmp_path: Path) -> None:
    ontology = _load_with_mappings(tmp_path, [_mapping()])

    assert isinstance(ontology.predicate_mappings[0], PredicateMapping)

    assert ontology.resolve_predicate("Person", "employed_by", "Company") == "works_at"
    assert ontology.resolve_predicate("Company", "employed_by", "Person") == "employed_by"
    assert ontology.resolve_predicate("Person", "advises", "Company") == "advises"

    assert not ontology.is_valid_pattern("Person", "employed_by", "Company")
    assert ontology.is_valid_pattern("Person", "works_at", "Company")


@pytest.mark.parametrize(
    "mapping",
    [
        _mapping(maps_to="nonexistent"),
        _mapping(maps_to="acquired"),
        _mapping(subject_type="Ghost"),
        _mapping(object_type="Ghost"),
        _mapping(observed_predicate="works_at"),
        _mapping(observed_predicate="DROP TABLE x;"),
    ],
)
def test_invalid_mapping_is_rejected(tmp_path: Path, mapping: dict[str, str]) -> None:
    with pytest.raises(OntologyLoadError):
        _load_with_mappings(tmp_path, [mapping])


def test_duplicate_mapping_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(OntologyLoadError, match="duplicate mapping"):
        _load_with_mappings(tmp_path, [_mapping(), _mapping()])


def test_mapping_round_trips_through_yaml(tmp_path: Path) -> None:
    ontology = _load_with_mappings(tmp_path, [_mapping()])

    path = tmp_path / "roundtrip.yaml"
    path.write_text(yaml.safe_dump(ontology.model_dump(mode="json")))

    assert load_ontology(path) == ontology
