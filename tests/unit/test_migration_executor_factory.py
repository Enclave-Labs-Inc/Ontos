"""M3-1 verified executor initialization and strict-mode integration."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from ontos.audit.emitter import AuditEmitter
from ontos.migration.loading import MigrationConfigurationError
from ontos.migration.records import AddPatternChange, MigrationRecord, load_migration_record
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    RelationType,
    load_ontology,
)
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.migration_executor import build_verified_executor
from ontos.runtime.migration_view import build_migration_view
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.runtime.ontology_approval import _audit_approval
from ontos.storage.networkx_store import NetworkxStore


def _config(monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    monkeypatch.delenv("ONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_HISTORY_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_PATH", raising=False)
    return RuntimeConfig.from_env()


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


def _setup_artifacts(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Ontology]:
    history = tmp_path / "history"
    migrations = tmp_path / "migrations"
    history.mkdir()
    migrations.mkdir()

    base = _base()

    (history / "ontology-0.1.yaml").write_text(yaml.safe_dump(base.model_dump(mode="json")))

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

    active_path = tmp_path / "active.yaml"

    active = write_ontology_version(
        base=base,
        record=record,
        ontology_path=active_path,
        migration_path=migrations / "0.1-to-0.2.yaml",
    )

    assert load_ontology(active_path) == active

    return active_path, migrations, history, active


async def _approve_migration_fixture(
    migrations: Path,
) -> AuditEmitter:
    """Create a genuine approval for this fixture's migration record."""
    migration_path = migrations / "0.1-to-0.2.yaml"
    record = load_migration_record(migration_path)

    assert len(record.approved_changes) == 1
    change = record.approved_changes[0]
    assert isinstance(change, AddPatternChange)

    audit = AuditEmitter(signing_key=b"m3-executor-migration-test")

    approval = await _audit_approval(
        audit=audit,
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        ontology_id=record.ontology_id,
        base_version=record.from_version,
        new_version=record.to_version,
        requested_change=change,
        acl_ref="acl:legal",
        decision="approval_granted",
        support_count=1,
        supporting_candidate_refs=["a" * 64],
    )

    # Update only the approval reference. The ontology history and
    # migration change remain identical to the original fixture.
    approved_record = record.model_copy(update={"approval_query_id": approval.query_id})
    migration_path.write_text(yaml.safe_dump(approved_record.model_dump(mode="json")))

    assert audit.verify_chain()
    return audit


def _fact(*, version: str = "0.1") -> Fact:
    now = datetime.now(UTC)

    return Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="employment.txt",
            extractor_id="test-extractor",
            extractor_version="1.0",
            ontology_id="ontos.test",
            ontology_version=version,
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


def _plan() -> Plan:
    return Plan(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(direction="out", depth=1)],
    )


async def test_verified_executor_keeps_historical_fact_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_path, migrations, history, _ = _setup_artifacts(tmp_path)

    config = replace(
        _config(monkeypatch),
        ontology_path=active_path,
        migration_dir=migrations,
        ontology_history_dir=history,
    )

    audit = await _approve_migration_fixture(migrations)
    view = build_migration_view(config)
    assert view is not None
    assert view.registry.strict

    # This is the same verification that production callers must
    # complete before executing migration-backed queries.
    await view.verify_approvals(audit)

    executor = build_verified_executor(config, view=view)
    stored = _fact()
    store = NetworkxStore()

    try:
        await store.add_fact(stored)
        result = await executor.execute(_plan(), store)

        assert len(result.hits) == 1
        assert result.hits[0].fact.id == stored.id
        assert result.hits[0].fact.predicate == "works_at"
        assert result.hits[0].fact.provenance.ontology_version == "0.1"
        assert not any("no-migration-path" in w for w in result.warnings)

        # No stored fact was rewritten.
        assert await store.get_fact(stored.id) == stored
    finally:
        await store.close()


async def test_verified_executor_drops_fact_without_migration_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_path, migrations, history, _ = _setup_artifacts(tmp_path)

    config = replace(
        _config(monkeypatch),
        ontology_path=active_path,
        migration_dir=migrations,
        ontology_history_dir=history,
    )

    audit = await _approve_migration_fixture(migrations)
    view = build_migration_view(config)
    assert view is not None
    assert view.registry.strict

    # This is the same verification that production callers must
    # complete before executing migration-backed queries.
    await view.verify_approvals(audit)

    executor = build_verified_executor(config, view=view)

    # The registry has a verified 0.1 -> 0.2 edge,
    # but no route from 0.0 to 0.2.
    stored = _fact(version="0.0")
    store = NetworkxStore()

    try:
        await store.add_fact(stored)
        result = await executor.execute(_plan(), store)

        assert result.hits == []
        assert any("no-migration-path" in w for w in result.warnings)
        assert await store.get_fact(stored.id) == stored
    finally:
        await store.close()


async def test_unconfigured_executor_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(monkeypatch)
    executor = build_verified_executor(config)
    stored = _fact(version="0.0")
    store = NetworkxStore()

    try:
        await store.add_fact(stored)
        result = await executor.execute(_plan(), store)

        assert len(result.hits) == 1
        assert result.hits[0].fact.id == stored.id
    finally:
        await store.close()


def test_explicit_ontology_overrides_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, migrations, history, active = _setup_artifacts(tmp_path)

    config = replace(
        _config(monkeypatch),
        ontology_path=tmp_path / "does-not-exist.yaml",
        migration_dir=migrations,
        ontology_history_dir=history,
    )

    # CLI --ontology can provide an already-loaded ontology.
    executor = build_verified_executor(
        config,
        active_ontology=active,
    )

    assert executor is not None


def test_partial_programmatic_configuration_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _config(monkeypatch),
        migration_dir=tmp_path / "migrations",
        ontology_history_dir=None,
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="both migration",
    ):
        build_verified_executor(config)


def test_configured_migrations_require_active_ontology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _config(monkeypatch),
        ontology_path=None,
        migration_dir=tmp_path / "migrations",
        ontology_history_dir=tmp_path / "history",
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="active ontology is required",
    ):
        build_verified_executor(config)


def test_invalid_history_never_falls_back_to_plain_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_path, migrations, history, _ = _setup_artifacts(tmp_path)

    # Delete the historical version required to justify the identity edge.
    (history / "ontology-0.1.yaml").unlink()

    config = replace(
        _config(monkeypatch),
        ontology_path=active_path,
        migration_dir=migrations,
        ontology_history_dir=history,
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="history verification failed",
    ):
        build_verified_executor(config)
