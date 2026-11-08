"""M3-1: verified historical migrations through the real MCP ask tool."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastmcp import Client

import ontos.llm
import ontos.planner
from ontos.audit.emitter import AuditEmitter
from ontos.migration.approval_audit import sign_migration_record
from ontos.migration.records import AddPatternChange, MigrationRecord
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.runtime.ontology_approval import record_ontology_approval_event
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore


async def _artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RuntimeConfig, Path, AuditEmitter]:
    monkeypatch.delenv("ONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_HISTORY_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_PATH", raising=False)
    monkeypatch.setenv("ONTOS_AUTHZ_BACKEND", "none")

    history = tmp_path / "history"
    migrations = tmp_path / "migrations"
    history.mkdir()
    migrations.mkdir()

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

    (history / "ontology-0.1.yaml").write_text(yaml.safe_dump(base.model_dump(mode="json")))

    # Use a genuine Article-12 approval for this migration.
    # The server receives this same audit sink at startup.
    audit = AuditEmitter(signing_key=b"m3-mcp-test")
    approval = await record_ontology_approval_event(
        audit=audit,
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        ontology_id=base.id,
        base_version="0.1",
        new_version="0.2",
        requested_change=AddPatternChange(
            action="add_pattern",
            subject_type="Person",
            predicate="acquired",
            object_type="Company",
        ),
        acl_ref="acl:legal",
        decision="approval_granted",
        support_count=1,
        supporting_candidate_refs=["a" * 64],
    )

    record = MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": base.id,
            "from_version": "0.1",
            "to_version": "0.2",
            "migration_kind": "identity",
            "approval_query_id": str(approval.query_id),
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

    # Sign the migration using the key supplied to the MCP server.
    record = sign_migration_record(record=record, audit=audit)

    active_path = tmp_path / "active.yaml"
    write_ontology_version(
        base=base,
        record=record,
        ontology_path=active_path,
        migration_path=migrations / "migration.yaml",
    )

    config = replace(
        RuntimeConfig.from_env(),
        planner_backend="ollama",
        ontology_path=active_path,
        migration_dir=migrations,
        ontology_history_dir=history,
    )

    return config, active_path, audit


def _fact(version: str) -> Fact:
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
            ontology_version=version,
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


def _stub_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Ontology]:
    pinned: list[Ontology] = []

    class StubLLM:
        def __init__(
            self,
            *args: object,
            **kwargs: object,
        ) -> None:
            pass

    class RecordingPlanner:
        id = "recording-planner"

        def __init__(
            self,
            llm: object,
            ontology: Ontology,
        ) -> None:
            self.ontology = ontology
            pinned.append(ontology)

        async def plan(
            self,
            question: str,
            ontology: Ontology,
        ) -> Plan:
            # Each request must use the ontology verified at startup.
            assert ontology is self.ontology
            assert ontology.version == "0.2"

            return Plan(
                seed=SeedByEntity(entity_id="person:alice"),
                steps=[TraversalStep(direction="out", depth=1)],
            )

    monkeypatch.setattr(ontos.llm, "OllamaBackend", StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", RecordingPlanner)

    return pinned


async def test_mcp_keeps_historical_fact_visible_with_pinned_ontology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, active_path, audit = await _artifacts(tmp_path, monkeypatch)
    pinned = _stub_planner(monkeypatch)

    store = NetworkxStore()
    stored = _fact("0.1")
    await store.add_fact(stored)

    server = build_server(
        config,
        store=store,
        audit=audit,
    )

    assert len(pinned) == 1
    assert pinned[0].version == "0.2"

    # Simulate a file change after startup. The running server must
    # continue using the previously verified ontology.
    active_path.write_text("not: a valid ontology\n")

    try:
        async with Client(server) as client:
            result = await client.call_tool(
                "ask",
                {
                    "question": "Who works at Acme?",
                    "agent_identity": "test",
                },
            )

        payload = result.data.payload

        assert len(payload["hits"]) == 1
        assert payload["hits"][0]["fact"]["id"] == str(stored.id)
        assert payload["warnings"] == []
        assert len(pinned) == 1

        # Query-time migrations do not rewrite stored facts.
        assert await store.get_fact(stored.id) == stored
    finally:
        await store.close()


async def test_mcp_drops_fact_without_verified_migration_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, audit = await _artifacts(tmp_path, monkeypatch)
    _stub_planner(monkeypatch)

    store = NetworkxStore()
    stored = _fact("0.0")
    await store.add_fact(stored)

    server = build_server(
        config,
        store=store,
        audit=audit,
    )

    try:
        async with Client(server) as client:
            result = await client.call_tool(
                "ask",
                {
                    "question": "Who works at Acme?",
                    "agent_identity": "test",
                },
            )

        payload = result.data.payload

        assert payload["hits"] == []
        assert any("no-migration-path" in warning for warning in payload["warnings"])

        # Even rejected projections leave storage unchanged.
        assert await store.get_fact(stored.id) == stored
    finally:
        await store.close()
