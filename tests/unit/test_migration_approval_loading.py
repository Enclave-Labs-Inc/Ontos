"""M3-2 migration loading requires genuine audit-backed approvals."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.migration.approval_audit import MigrationApprovalAuditError
from ontos.migration.loading import (
    load_audit_verified_migration_registry,
)
from ontos.migration.records import AddPatternChange, MigrationRecord
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.migration_executor import build_verified_executor
from ontos.runtime.migration_view import build_migration_view
from ontos.runtime.ontology_approval import _audit_approval
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore


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


def _change() -> AddPatternChange:
    return AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )


async def _artifacts(
    tmp_path: Path,
    *,
    forged: bool = False,
) -> tuple[Path, Path, Ontology, AuditEmitter]:
    history = tmp_path / "history"
    migrations = tmp_path / "migrations"
    history.mkdir()
    migrations.mkdir()

    base = _base()
    (history / "ontology-0.1.yaml").write_text(yaml.safe_dump(base.model_dump(mode="json")))

    audit = AuditEmitter(b"m3-2-verified-loading-test-key")

    event = await _audit_approval(
        audit=audit,
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        ontology_id=base.id,
        base_version="0.1",
        new_version="0.2",
        requested_change=_change(),
        acl_ref="acl:legal",
        decision="approval_granted",
        support_count=1,
        supporting_candidate_refs=["a" * 64],
    )

    record = MigrationRecord(
        schema_version=1,
        ontology_id=base.id,
        from_version="0.1",
        to_version="0.2",
        migration_kind="identity",
        approval_query_id=uuid4() if forged else event.query_id,
        approved_changes=[_change()],
    )

    active = write_ontology_version(
        base=base,
        record=record,
        ontology_path=tmp_path / "active.yaml",
        migration_path=migrations / "migration.yaml",
    )

    return migrations, history, active, audit


async def test_valid_approval_allows_verified_registry(
    tmp_path: Path,
) -> None:
    migrations, history, active, audit = await _artifacts(tmp_path)

    registry = await load_audit_verified_migration_registry(
        migrations,
        active_ontology=active,
        historical_ontology_dir=history,
        audit=audit,
        strict=True,
    )

    assert registry is not None
    assert audit.verify_chain()


async def test_forged_id_refuses_verified_registry(
    tmp_path: Path,
) -> None:
    migrations, history, active, audit = await _artifacts(
        tmp_path,
        forged=True,
    )

    with pytest.raises(
        MigrationApprovalAuditError,
        match="not found",
    ):
        await load_audit_verified_migration_registry(
            migrations,
            active_ontology=active,
            historical_ontology_dir=history,
            audit=audit,
            strict=True,
        )


async def test_broken_audit_chain_refuses_verified_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations, history, active, audit = await _artifacts(tmp_path)

    monkeypatch.setattr(audit, "verify_chain", lambda: False)

    with pytest.raises(
        MigrationApprovalAuditError,
        match="chain failed",
    ):
        await load_audit_verified_migration_registry(
            migrations,
            active_ontology=active,
            historical_ontology_dir=history,
            audit=audit,
            strict=True,
        )


def _view_config(
    monkeypatch: pytest.MonkeyPatch,
    migrations: Path,
    history: Path,
) -> RuntimeConfig:
    monkeypatch.delenv("ONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_HISTORY_DIR", raising=False)
    return replace(
        RuntimeConfig.from_env(),
        migration_dir=migrations,
        ontology_history_dir=history,
    )


async def test_unverified_view_refuses_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations, history, active, audit = await _artifacts(tmp_path)

    view = build_migration_view(
        _view_config(monkeypatch, migrations, history),
        active_ontology=active,
    )

    assert view is not None
    assert view.registry.strict

    with pytest.raises(
        MigrationApprovalAuditError,
        match="not been verified",
    ):
        await view.project([])

    await view.verify_approvals(audit)

    assert await view.project([]) == []
    assert audit.verify_chain()


async def test_forged_view_never_opens_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations, history, active, audit = await _artifacts(
        tmp_path,
        forged=True,
    )

    view = build_migration_view(
        _view_config(monkeypatch, migrations, history),
        active_ontology=active,
    )

    assert view is not None

    with pytest.raises(
        MigrationApprovalAuditError,
        match="not found",
    ):
        await view.verify_approvals(audit)

    with pytest.raises(
        MigrationApprovalAuditError,
        match="not been verified",
    ):
        await view.project([])


def _contains_migration_approval_error(
    exc: BaseException,
) -> bool:
    if isinstance(exc, MigrationApprovalAuditError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_migration_approval_error(child) for child in exc.exceptions)
    if exc.__cause__ is not None:
        return _contains_migration_approval_error(exc.__cause__)
    if exc.__context__ is not None:
        return _contains_migration_approval_error(exc.__context__)
    return False


@pytest.mark.parametrize("forged", [False, True])
async def test_server_startup_requires_genuine_migration_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged: bool,
) -> None:
    migrations, history, active, audit = await _artifacts(
        tmp_path,
        forged=forged,
    )

    config = replace(
        _view_config(monkeypatch, migrations, history),
        ontology_path=tmp_path / "active.yaml",
    )

    view = build_migration_view(
        config,
        active_ontology=active,
    )
    assert view is not None
    assert view.verification_gate is not None
    assert not view.verification_gate.verified

    # Avoid requiring a live LLM for a migration startup test.
    monkeypatch.setattr(
        "ontos.runtime.server.build_planner",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ontos.runtime.server.build_migration_view",
        lambda *args, **kwargs: view,
    )

    store = NetworkxStore()
    server = build_server(
        config,
        store=store,
        audit=audit,
    )

    try:
        if forged:
            with pytest.raises(Exception) as captured:
                async with Client(server):
                    pass

            assert _contains_migration_approval_error(captured.value)
            assert not view.verification_gate.verified
        else:
            async with Client(server):
                assert view.verification_gate.verified

            assert audit.verify_chain()
    finally:
        await store.close()


@pytest.mark.parametrize("forged", [False, True])
async def test_executor_requires_verified_approval_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged: bool,
) -> None:
    migrations, history, active, audit = await _artifacts(
        tmp_path,
        forged=forged,
    )

    config = _view_config(monkeypatch, migrations, history)

    view = build_migration_view(
        config,
        active_ontology=active,
    )
    assert view is not None

    executor = build_verified_executor(
        config,
        view=view,
    )

    plan = Plan(
        seed=SeedByEntity(entity_id="person:missing"),
        steps=[TraversalStep(direction="out", depth=1)],
    )

    store = NetworkxStore()
    try:
        # Verification is required even if this query has no hits.
        with pytest.raises(
            MigrationApprovalAuditError,
            match="not been verified",
        ):
            await executor.execute(plan, store)

        if forged:
            with pytest.raises(
                MigrationApprovalAuditError,
                match="not found",
            ):
                await view.verify_approvals(audit)

            # A failed verification never unlocks the executor.
            with pytest.raises(
                MigrationApprovalAuditError,
                match="not been verified",
            ):
                await executor.execute(plan, store)
        else:
            await view.verify_approvals(audit)

            result = await executor.execute(plan, store)
            assert result.hits == []
            assert audit.verify_chain()
    finally:
        await store.close()
