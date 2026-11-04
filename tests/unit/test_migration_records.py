"""M3-1 declarative migration-record validation tests."""

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from ontos.migration.records import (
    MigrationRecordLoadError,
    load_migration_record,
    register_migration_record,
)


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "ontology_id": "ontos.starter",
        "from_version": "0.1",
        "to_version": "0.2",
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


def _load(tmp_path: Path, record: dict[str, object]):
    path = tmp_path / "migration.yaml"
    path.write_text(yaml.safe_dump(record))
    return load_migration_record(path)


def test_identity_migration_record_loads(tmp_path: Path) -> None:
    record = _load(tmp_path, _record())

    assert record.schema_version == 1
    assert record.migration_kind == "identity"
    assert record.from_version == "0.1"
    assert record.to_version == "0.2"
    assert record.approved_changes[0].action == "map_to_existing"


@pytest.mark.parametrize(
    "change",
    [
        {
            "action": "add_pattern",
            "subject_type": "Person",
            "predicate": "advises",
            "object_type": "Company",
        },
        {
            "action": "new_relation_type",
            "subject_type": "Person",
            "relation_type": "mentors",
            "object_type": "Person",
            "cardinality": "many_to_many",
        },
    ],
)
def test_other_approved_change_types_load(
    tmp_path: Path,
    change: dict[str, str],
) -> None:
    payload = _record()
    payload["approved_changes"] = [change]

    loaded = _load(tmp_path, payload)
    assert loaded.approved_changes[0].action == change["action"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("migration_kind", "predicate_rename"),
        ("approval_query_id", "not-a-uuid"),
        ("approved_changes", []),
        ("ontology_id", ""),
        ("from_version", " "),
        ("to_version", "0.1"),
    ],
)
def test_invalid_record_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _record()
    payload[field] = value

    with pytest.raises(MigrationRecordLoadError):
        _load(tmp_path, payload)


def test_unknown_change_type_is_rejected(tmp_path: Path) -> None:
    payload = _record()
    payload["approved_changes"] = [{"action": "execute_python"}]

    with pytest.raises(MigrationRecordLoadError):
        _load(tmp_path, payload)


def test_unknown_record_fields_are_rejected(tmp_path: Path) -> None:
    payload = _record()
    payload["unexpected"] = "value"

    with pytest.raises(MigrationRecordLoadError):
        _load(tmp_path, payload)


def test_unknown_change_fields_are_rejected(tmp_path: Path) -> None:
    payload = _record()
    change = {
        "action": "add_pattern",
        "subject_type": "Person",
        "predicate": "advises",
        "object_type": "Company",
        "unexpected": "value",
    }
    payload["approved_changes"] = [change]

    with pytest.raises(MigrationRecordLoadError):
        _load(tmp_path, payload)


def test_missing_record_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(MigrationRecordLoadError):
        load_migration_record(tmp_path / "missing.yaml")


def test_non_mapping_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("- item\n- item\n")

    with pytest.raises(MigrationRecordLoadError):
        load_migration_record(path)


def _historical_fact():
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance

    now = datetime.now(UTC)
    return Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="employment.txt",
            extractor_id="test",
            extractor_version="1.0",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


def _target(version: str):
    from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType

    return Ontology(
        id="ontos.starter",
        version=version,
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
    )


async def test_identity_record_keeps_old_fact_visible_in_strict_mode(
    tmp_path: Path,
) -> None:
    from ontos.migration import MigrationRegistry

    record = _load(tmp_path, _record())
    registry = MigrationRegistry(strict=True)
    register_migration_record(registry, record)

    stored = _historical_fact()
    migrated, warnings = await registry.migrate_fact(stored, _target("0.2"))

    assert migrated is stored
    assert warnings == []
    assert migrated.provenance.ontology_version == "0.1"
    assert migrated.predicate == "works_at"

    path = registry.path("0.1", "0.2")
    assert path is not None
    assert len(path) == 1
    assert str(record.approval_query_id) in path[0].id


async def test_missing_identity_record_drops_fact_in_strict_mode() -> None:
    from ontos.migration import MigrationRegistry

    registry = MigrationRegistry(strict=True)
    stored = _historical_fact()

    migrated, warnings = await registry.migrate_fact(stored, _target("0.2"))

    assert migrated is None
    assert len(warnings) == 1
    assert warnings[0].reason == "no-migration-path"


async def test_identity_records_chain_across_versions(
    tmp_path: Path,
) -> None:
    from ontos.migration import MigrationRegistry

    first = _load(tmp_path, _record())

    second_payload = _record()
    second_payload["from_version"] = "0.2"
    second_payload["to_version"] = "0.3"
    second = _load(tmp_path, second_payload)

    registry = MigrationRegistry(strict=True)
    register_migration_record(registry, first)
    register_migration_record(registry, second)

    stored = _historical_fact()
    migrated, warnings = await registry.migrate_fact(stored, _target("0.3"))

    assert migrated is stored
    assert warnings == []

    path = registry.path("0.1", "0.3")
    assert path is not None
    assert len(path) == 2
    assert path[0].id != path[1].id
