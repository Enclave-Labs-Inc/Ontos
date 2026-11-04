"""M3-1: non-overwriting artifact publication and rollback races."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

import ontos.ontology.artifact_writer as writer
from ontos.migration.records import MigrationRecord
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType


def _inputs() -> tuple[Ontology, MigrationRecord]:
    base = Ontology(
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

    record = MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": base.id,
            "from_version": "0.1",
            "to_version": "0.2",
            "migration_kind": "identity",
            "approval_query_id": str(uuid4()),
            "approved_changes": [
                {
                    "action": "add_pattern",
                    "subject_type": "Person",
                    "predicate": "acquired",
                    "object_type": "Company",
                }
            ],
        }
    )

    return base, record


def _publish(tmp_path: Path) -> tuple[Path, Path]:
    return (
        tmp_path / "ontology-0.2.yaml",
        tmp_path / "migration.yaml",
    )


def _assert_no_temporary_files(tmp_path: Path) -> None:
    assert list(tmp_path.glob(".*.tmp")) == []


def test_competing_first_artifact_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, record = _inputs()
    ontology_path, migration_path = _publish(tmp_path)

    original_link = os.link

    def competing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(destination) == ontology_path:
            ontology_path.write_text("other publisher")
        original_link(source, destination)

    monkeypatch.setattr(writer.os, "link", competing_link)

    with pytest.raises(FileExistsError):
        writer.write_ontology_version(
            base=base,
            record=record,
            ontology_path=ontology_path,
            migration_path=migration_path,
        )

    assert ontology_path.read_text() == "other publisher"
    assert not migration_path.exists()
    _assert_no_temporary_files(tmp_path)


def test_competing_second_artifact_survives_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, record = _inputs()
    ontology_path, migration_path = _publish(tmp_path)

    original_link = os.link

    def competing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(destination) == migration_path:
            migration_path.write_text("other publisher")
        original_link(source, destination)

    monkeypatch.setattr(writer.os, "link", competing_link)

    with pytest.raises(FileExistsError):
        writer.write_ontology_version(
            base=base,
            record=record,
            ontology_path=ontology_path,
            migration_path=migration_path,
        )

    # Our first artifact was rolled back; the competing file remains.
    assert not ontology_path.exists()
    assert migration_path.read_text() == "other publisher"
    _assert_no_temporary_files(tmp_path)


def test_rollback_preserves_replacement_of_first_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, record = _inputs()
    ontology_path, migration_path = _publish(tmp_path)

    original_link = os.link

    def competing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(destination) == migration_path:
            # Replace the first published artifact before forcing
            # the second publication to fail.
            ontology_path.unlink()
            ontology_path.write_text("replacement owned by another process")
            raise FileExistsError("simulated second publication failure")

        original_link(source, destination)

    monkeypatch.setattr(writer.os, "link", competing_link)

    with pytest.raises(FileExistsError):
        writer.write_ontology_version(
            base=base,
            record=record,
            ontology_path=ontology_path,
            migration_path=migration_path,
        )

    # The replacement has a different inode and must be preserved.
    assert ontology_path.read_text() == "replacement owned by another process"
    assert not migration_path.exists()
    _assert_no_temporary_files(tmp_path)
