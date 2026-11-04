"""M3-1 validation of historical ontology versions."""

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from ontos.migration.history import (
    MigrationHistoryError,
    verify_migration_history,
)
from ontos.migration.records import MigrationRecord
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    RelationType,
)
from ontos.ontology.versioning import build_next_ontology


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
            )
        ],
    )


def _record(
    from_version: str,
    to_version: str,
    change: dict[str, str],
) -> MigrationRecord:
    return MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": "ontos.test",
            "from_version": from_version,
            "to_version": to_version,
            "migration_kind": "identity",
            "approval_query_id": str(uuid4()),
            "approved_changes": [change],
        }
    )


def _mapping() -> dict[str, str]:
    return {
        "action": "map_to_existing",
        "subject_type": "Person",
        "observed_predicate": "employed_by",
        "object_type": "Company",
        "maps_to": "works_at",
    }


def _add_pattern() -> dict[str, str]:
    return {
        "action": "add_pattern",
        "subject_type": "Person",
        "predicate": "acquired",
        "object_type": "Company",
    }


def _save(directory: Path, ontology: Ontology) -> None:
    path = directory / f"ontology-{ontology.version}.yaml"
    path.write_text(yaml.safe_dump(ontology.model_dump(mode="json")))


def _history(tmp_path: Path):
    directory = tmp_path / "historical"
    directory.mkdir()

    base = _base()
    first = _record("0.1", "0.2", _mapping())
    second = _record("0.2", "0.3", _add_pattern())

    v02 = build_next_ontology(base, first)
    v03 = build_next_ontology(v02, second)

    _save(directory, base)
    _save(directory, v02)

    return directory, base, v02, v03, first, second


def test_valid_two_step_history_is_accepted(
    tmp_path: Path,
) -> None:
    directory, _, _, active, first, second = _history(tmp_path)

    verify_migration_history(
        records=[first, second],
        historical_ontology_dir=directory,
        active_ontology=active,
    )


def test_unrecorded_schema_change_is_rejected(
    tmp_path: Path,
) -> None:
    directory, _, _, active, first, second = _history(tmp_path)

    # An identity migration cannot conceal an additional schema change.
    altered = active.model_copy(
        update={
            "relation_types": [
                *active.relation_types,
                RelationType(label="unrecorded_relation"),
            ]
        }
    )

    with pytest.raises(MigrationHistoryError, match="unrecorded changes"):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=altered,
        )


def test_missing_intermediate_ontology_is_rejected(
    tmp_path: Path,
) -> None:
    directory, _, _, active, first, second = _history(tmp_path)
    (directory / "ontology-0.2.yaml").unlink()

    with pytest.raises(MigrationHistoryError, match="missing historical"):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=active,
        )


def test_missing_source_ontology_is_rejected(
    tmp_path: Path,
) -> None:
    directory, _, _, active, first, second = _history(tmp_path)
    (directory / "ontology-0.1.yaml").unlink()

    with pytest.raises(
        MigrationHistoryError,
        match="missing historical source",
    ):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=active,
        )


def test_wrong_ontology_family_is_rejected(
    tmp_path: Path,
) -> None:
    directory, base, _, active, first, second = _history(tmp_path)

    _save(
        directory,
        base.model_copy(update={"id": "unrelated.ontology", "version": "0.9"}),
    )

    with pytest.raises(MigrationHistoryError, match="family mismatch"):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=active,
        )


def test_duplicate_version_is_rejected(
    tmp_path: Path,
) -> None:
    directory, base, _, active, first, second = _history(tmp_path)

    duplicate = directory / "duplicate.yaml"
    duplicate.write_text(yaml.safe_dump(base.model_dump(mode="json")))

    with pytest.raises(MigrationHistoryError, match="duplicate"):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=active,
        )


def test_invalid_historical_yaml_is_rejected(
    tmp_path: Path,
) -> None:
    directory, _, _, active, first, second = _history(tmp_path)
    (directory / "broken.yaml").write_text("not: an ontology\n")

    with pytest.raises(MigrationHistoryError, match="invalid historical"):
        verify_migration_history(
            records=[first, second],
            historical_ontology_dir=directory,
            active_ontology=active,
        )


def test_missing_history_directory_is_rejected(
    tmp_path: Path,
) -> None:
    active = _base()

    with pytest.raises(MigrationHistoryError, match="does not exist"):
        verify_migration_history(
            records=[],
            historical_ontology_dir=tmp_path / "missing",
            active_ontology=active,
        )
