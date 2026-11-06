"""M3-1 migration-directory loading and strict-mode tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from ontos.migration.loading import (
    MigrationConfigurationError,
    load_migration_registry,
)
from ontos.migration.records import MigrationRecord, load_migration_record
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    PredicateMapping,
    RelationType,
)
from ontos.ontology.versioning import build_next_ontology
from ontos.runtime.models import Confidence, Fact, Provenance


def _ontology(version: str = "0.3") -> Ontology:
    return Ontology(
        id="ontos.test",
        version=version,
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
                subject_type="Person",
                predicate="acquired",
                object_type="Company",
            ),
        ],
        predicate_mappings=[
            PredicateMapping(
                subject_type="Person",
                observed_predicate="employed_by",
                object_type="Company",
                maps_to="works_at",
            )
        ],
    )


def _write_record(
    directory: Path,
    from_version: str,
    to_version: str,
    *,
    ontology_id: str = "ontos.test",
    filename: str | None = None,
) -> Path:
    if from_version == "0.1":
        change = {
            "action": "map_to_existing",
            "subject_type": "Person",
            "observed_predicate": "employed_by",
            "object_type": "Company",
            "maps_to": "works_at",
        }
    else:
        change = {
            "action": "add_pattern",
            "subject_type": "Person",
            "predicate": "acquired",
            "object_type": "Company",
        }

    record = MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": ontology_id,
            "from_version": from_version,
            "to_version": to_version,
            "migration_kind": "identity",
            "approval_query_id": str(uuid4()),
            "approved_changes": [change],
        }
    )

    path = directory / (filename or f"migration-{from_version}-to-{to_version}.yaml")
    path.write_text(yaml.safe_dump(record.model_dump(mode="json")))
    return path


def _write_history(directory: Path) -> Path:
    """Construct real historical versions matching the test records."""
    history = directory / "history"
    history.mkdir()

    base = _ontology("0.1").model_copy(
        update={
            "predicate_mappings": [],
            "patterns": [
                Pattern(
                    subject_type="Person",
                    predicate="works_at",
                    object_type="Company",
                )
            ],
        }
    )

    first = load_migration_record(directory / "migration-0.1-to-0.2.yaml")
    second = load_migration_record(directory / "migration-0.2-to-0.3.yaml")

    v02 = build_next_ontology(base, first)
    v03 = build_next_ontology(v02, second)

    assert v03 == _ontology(), "Test history does not match active ontology"

    for ontology in (base, v02):
        (history / f"ontology-{ontology.version}.yaml").write_text(
            yaml.safe_dump(ontology.model_dump(mode="json"))
        )

    return history


def _old_fact() -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="employment.txt",
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.test",
            ontology_version="0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def test_two_hop_chain_keeps_old_fact_visible(
    tmp_path: Path,
) -> None:
    _write_record(tmp_path, "0.1", "0.2")
    _write_record(tmp_path, "0.2", "0.3")
    _write_history(tmp_path)

    registry = load_migration_registry(
        tmp_path,
        active_ontology=_ontology(),
        historical_ontology_dir=tmp_path / "history",
    )

    assert registry.strict
    assert registry.path("0.1", "0.3") is not None
    assert len(registry.path("0.1", "0.3") or []) == 2

    stored = _old_fact()
    migrated, warnings = await registry.migrate_fact(
        stored,
        _ontology(),
    )

    assert migrated is stored
    assert warnings == []
    assert stored.provenance.ontology_version == "0.1"


def test_missing_intermediate_edge_is_refused(tmp_path: Path) -> None:
    _write_record(tmp_path, "0.1", "0.2")

    with pytest.raises(
        MigrationConfigurationError,
        match="does not reach active version",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_conflicting_source_versions_are_refused(
    tmp_path: Path,
) -> None:
    _write_record(tmp_path, "0.1", "0.2", filename="first.yaml")
    _write_record(tmp_path, "0.1", "0.3", filename="second.yaml")

    with pytest.raises(
        MigrationConfigurationError,
        match="duplicate or conflicting",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_wrong_ontology_family_is_refused(tmp_path: Path) -> None:
    _write_record(
        tmp_path,
        "0.1",
        "0.3",
        ontology_id="different.ontology",
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="ontology mismatch",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_future_edge_is_refused(tmp_path: Path) -> None:
    _write_record(tmp_path, "0.3", "0.4")

    with pytest.raises(
        MigrationConfigurationError,
        match="active ontology version.*must be terminal",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_missing_directory_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(
        MigrationConfigurationError,
        match="does not exist",
    ):
        load_migration_registry(
            tmp_path / "missing",
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_empty_directory_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(
        MigrationConfigurationError,
        match="no migration records",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_malformed_record_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("not: a migration record\n")

    with pytest.raises(
        MigrationConfigurationError,
        match="invalid migration configuration",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_missing_history_is_refused_even_with_valid_records(
    tmp_path: Path,
) -> None:
    _write_record(tmp_path, "0.1", "0.2")
    _write_record(tmp_path, "0.2", "0.3")

    with pytest.raises(
        MigrationConfigurationError,
        match="history verification failed",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "missing-history",
        )


def test_unrecorded_historical_change_blocks_registry_loading(
    tmp_path: Path,
) -> None:
    _write_record(tmp_path, "0.1", "0.2")
    _write_record(tmp_path, "0.2", "0.3")
    history = _write_history(tmp_path)

    # Introduce an unapproved modification into the intermediate
    # ontology. A structurally valid migration chain is not enough.
    path = history / "ontology-0.2.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["relation_types"][0]["description"] = "unapproved change"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(
        MigrationConfigurationError,
        match="history verification failed",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=history,
        )


def test_cycle_returning_to_active_version_is_refused(
    tmp_path: Path,
) -> None:
    """A 0.1 -> 0.2 -> 0.3 -> 0.1 cycle is never valid."""
    _write_record(tmp_path, "0.1", "0.2")
    _write_record(tmp_path, "0.2", "0.3")
    _write_record(tmp_path, "0.3", "0.1")

    with pytest.raises(
        MigrationConfigurationError,
        match="active ontology version.*must be terminal",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )


def test_cycle_through_future_version_is_refused(
    tmp_path: Path,
) -> None:
    """An active -> future -> active loop cannot be approved."""
    _write_record(tmp_path, "0.3", "0.4")
    _write_record(tmp_path, "0.4", "0.3")

    with pytest.raises(
        MigrationConfigurationError,
        match="active ontology version.*must be terminal",
    ):
        load_migration_registry(
            tmp_path,
            active_ontology=_ontology(),
            historical_ontology_dir=tmp_path / "history",
        )
