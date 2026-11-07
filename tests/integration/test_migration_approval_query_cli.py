"""M3-2: CLI query must verify migration approvals before graph access."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from typer.testing import CliRunner

import ontos.runtime.server as server_runtime
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.cli import app
from ontos.cli.main import _scan_for_export
from ontos.migration.approval_audit import MigrationApprovalAuditError
from ontos.migration.records import AddPatternChange, MigrationRecord
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.ontology_approval import _audit_approval

runner = CliRunner()


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


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    forged: bool,
) -> tuple[Path, Path]:
    history = tmp_path / "history"
    migrations = tmp_path / "migrations"
    history.mkdir()
    migrations.mkdir()

    base = _base()
    (history / "ontology-0.1.yaml").write_text(
        yaml.safe_dump(base.model_dump(mode="json")),
        encoding="utf-8",
    )

    db_path = tmp_path / "audit.sqlite"
    audit_url = f"sqlite+aiosqlite:///{db_path}"

    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", audit_url)
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m3-2-query-integration-test-key",
    )
    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", str(migrations))
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", str(history))

    async def create_approval():
        audit = PostgresAuditEmitter.from_url(audit_url)
        try:
            await audit.initialize()
            return await _audit_approval(
                audit=audit,
                subject="user:operator",
                agent_identity="ontos-cli-local-operator",
                ontology_id=base.id,
                base_version="0.1",
                new_version="0.2",
                requested_change=_change(),
                acl_ref="acl:legal",
                decision="approval_granted",
                supporting_candidate_refs=["a" * 64],
                support_count=1,
            )
        finally:
            await audit.close()

    approval_event = asyncio.run(create_approval())

    record = MigrationRecord(
        schema_version=1,
        ontology_id=base.id,
        from_version="0.1",
        to_version="0.2",
        migration_kind="identity",
        approval_query_id=(uuid4() if forged else approval_event.query_id),
        approved_changes=[_change()],
    )

    active_path = tmp_path / "active.yaml"
    write_ontology_version(
        base=base,
        record=record,
        ontology_path=active_path,
        migration_path=migrations / "migration.yaml",
    )

    return active_path, db_path


def _mock_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubPlanner:
        id = "test-query-planner"

        def __init__(
            self,
            llm: object,
            ontology: Ontology,
        ) -> None:
            pass

        async def plan(
            self,
            question: str,
            ontology: Ontology,
        ) -> Plan:
            return Plan(
                seed=SeedByEntity(entity_id="person:nonexistent"),
                steps=[
                    TraversalStep(
                        direction="out",
                        depth=1,
                    )
                ],
            )

    monkeypatch.setattr(
        "ontos.cli.main._build_llm",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "ontos.planner.LlmPlanner",
        StubPlanner,
    )


def _query_events(db_path: Path) -> list[dict[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT article12 FROM ontos_audit_records").fetchall()

    events = [json.loads(row[0]) for row in rows]
    return [
        event for event in events if event[ArticleTwelveField.TOOL_INVOKED.value] == "ontos.query"
    ]


@pytest.mark.parametrize("forged", [False, True])
def test_cli_query_requires_persisted_matching_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged: bool,
) -> None:
    active_path, db_path = _prepare(
        tmp_path,
        monkeypatch,
        forged=forged,
    )
    _mock_planner(monkeypatch)

    graph_initializations: list[object] = []
    original_initialize = server_runtime.initialize_store

    async def track_initialize(store: object) -> None:
        graph_initializations.append(store)
        await original_initialize(store)

    monkeypatch.setattr(
        server_runtime,
        "initialize_store",
        track_initialize,
    )

    result = runner.invoke(
        app,
        [
            "query",
            "Who works at the company?",
            "--ontology",
            str(active_path),
            "--storage-path",
            str(tmp_path / "graph.pkl"),
        ],
    )

    events = _query_events(db_path)
    assert len(events) == 1

    decisions = json.loads(events[0][ArticleTwelveField.POLICY_DECISIONS.value])

    if forged:
        assert result.exit_code != 0
        assert isinstance(
            result.exception,
            MigrationApprovalAuditError,
        )

        # The forged approval was refused before graph initialization.
        assert graph_initializations == []
        assert {
            "policy": "request",
            "result": "error",
        } in decisions
    else:
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert len(graph_initializations) == 1
        assert {
            "policy": "request",
            "result": "completed",
        } in decisions

    async def verify_audit() -> None:
        audit_url = f"sqlite+aiosqlite:///{db_path}"
        audit = PostgresAuditEmitter.from_url(audit_url)
        try:
            assert await audit.verify_chain_async()
            assert await audit.count_async() == 2
        finally:
            await audit.close()

    asyncio.run(verify_audit())


@pytest.mark.parametrize("forged", [False, True])
def test_export_requires_persisted_matching_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged: bool,
) -> None:
    active_path, db_path = _prepare(
        tmp_path,
        monkeypatch,
        forged=forged,
    )
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(active_path))
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")

    graph_initializations: list[object] = []
    original_initialize = server_runtime.initialize_store

    async def track_initialize(store: object) -> None:
        graph_initializations.append(store)
        await original_initialize(store)

    monkeypatch.setattr(
        server_runtime,
        "initialize_store",
        track_initialize,
    )

    storage_path = str(tmp_path / "graph.pkl")

    if forged:
        with pytest.raises(
            MigrationApprovalAuditError,
            match="not found",
        ):
            _scan_for_export(
                storage_path,
                export_format="graphml",
            )

        # A forged approval cannot reach graph initialization.
        assert graph_initializations == []
    else:
        scan = _scan_for_export(
            storage_path,
            export_format="graphml",
        )
        assert scan.facts == []
        assert len(graph_initializations) == 1

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT article12 FROM ontos_audit_records").fetchall()

    events = [json.loads(row[0]) for row in rows]
    export_events = [
        event
        for event in events
        if event[ArticleTwelveField.TOOL_INVOKED.value] == "ontos.export.graphml"
    ]

    assert len(export_events) == 1

    event = export_events[0]
    assert event[ArticleTwelveField.RESULT_FACT_IDS.value] in (
        [],
        "[]",
    )

    decisions = json.loads(event[ArticleTwelveField.POLICY_DECISIONS.value])

    if forged:
        assert {
            "policy": "ontology_approval",
            "result": "verification_failed",
        } in decisions
    else:
        assert not any(decision.get("result") == "verification_failed" for decision in decisions)

    async def verify_audit() -> None:
        audit_url = f"sqlite+aiosqlite:///{db_path}"
        audit = PostgresAuditEmitter.from_url(audit_url)
        try:
            assert await audit.verify_chain_async()
            assert await audit.count_async() == 2
        finally:
            await audit.close()

    asyncio.run(verify_audit())
