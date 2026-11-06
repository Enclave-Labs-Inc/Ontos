"""M3-1 safe, non-overwriting ontology artifact tests."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from ontos.migration.records import (
    MigrationRecord,
    load_migration_record,
)
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import load_ontology
from ontos.ontology.versioning import OntologyVersionError

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _record(base_version: str) -> MigrationRecord:
    return MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": "ontos.starter",
            "from_version": base_version,
            "to_version": "2.0",
            "migration_kind": "identity",
            "approval_query_id": str(uuid4()),
            "approved_changes": [
                {
                    "action": "map_to_existing",
                    "subject_type": "Person",
                    "observed_predicate": "employed_by",
                    "object_type": "Company",
                    "maps_to": "works_at",
                }
            ],
        }
    )


def test_versioned_artifacts_round_trip(tmp_path: Path) -> None:
    base = load_ontology(STARTER)
    record = _record(base.version)
    ontology_file = tmp_path / "ontology-v2.yaml"
    migration_file = tmp_path / "migration-to-v2.yaml"

    updated = write_ontology_version(
        base=base,
        record=record,
        ontology_path=ontology_file,
        migration_path=migration_file,
    )

    assert load_ontology(ontology_file) == updated
    assert load_migration_record(migration_file) == record

    assert updated.version == "2.0"
    assert base.version != updated.version
    assert base.predicate_mappings == []
    assert updated.resolve_predicate("Person", "employed_by", "Company") == "works_at"

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "migration-to-v2.yaml",
        "ontology-v2.yaml",
    ]


@pytest.mark.parametrize("existing", ["ontology", "migration"])
def test_existing_artifact_is_never_overwritten(
    tmp_path: Path,
    existing: str,
) -> None:
    base = load_ontology(STARTER)
    ontology_file = tmp_path / "ontology.yaml"
    migration_file = tmp_path / "migration.yaml"

    occupied = ontology_file if existing == "ontology" else migration_file
    occupied.write_text("KEEP ORIGINAL CONTENT")

    with pytest.raises(FileExistsError):
        write_ontology_version(
            base=base,
            record=_record(base.version),
            ontology_path=ontology_file,
            migration_path=migration_file,
        )

    assert occupied.read_text() == "KEEP ORIGINAL CONTENT"
    other = migration_file if existing == "ontology" else ontology_file
    assert not other.exists()


def test_stale_base_creates_no_files(tmp_path: Path) -> None:
    base = load_ontology(STARTER)
    record = _record("not-the-current-version")

    with pytest.raises(OntologyVersionError, match="stale"):
        write_ontology_version(
            base=base,
            record=record,
            ontology_path=tmp_path / "new.yaml",
            migration_path=tmp_path / "migration.yaml",
        )

    assert list(tmp_path.iterdir()) == []


def test_second_publish_failure_rolls_back_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.ontology import artifact_writer

    base = load_ontology(STARTER)
    ontology_file = tmp_path / "new.yaml"
    migration_file = tmp_path / "migration.yaml"

    original_link = os.link

    def fail_second(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == migration_file:
            raise OSError("simulated second publish failure")
        original_link(src, dst)

    monkeypatch.setattr(artifact_writer.os, "link", fail_second)

    with pytest.raises(OSError, match="simulated"):
        write_ontology_version(
            base=base,
            record=_record(base.version),
            ontology_path=ontology_file,
            migration_path=migration_file,
        )

    assert list(tmp_path.iterdir()) == []


def test_same_destination_is_refused(tmp_path: Path) -> None:
    base = load_ontology(STARTER)
    destination = tmp_path / "same.yaml"

    with pytest.raises(ValueError, match="different"):
        write_ontology_version(
            base=base,
            record=_record(base.version),
            ontology_path=destination,
            migration_path=destination,
        )

    assert not destination.exists()


def test_dangling_symlink_cannot_be_overwritten(
    tmp_path: Path,
) -> None:
    base = load_ontology(STARTER)
    ontology_file = tmp_path / "ontology.yaml"
    ontology_file.symlink_to(tmp_path / "missing.yaml")

    with pytest.raises(FileExistsError):
        write_ontology_version(
            base=base,
            record=_record(base.version),
            ontology_path=ontology_file,
            migration_path=tmp_path / "migration.yaml",
        )

    assert ontology_file.is_symlink()
    assert not (tmp_path / "migration.yaml").exists()
