"""Gate-blocking: verified migrations apply to every read surface (#112).

M3-1 hid a fact without a verified migration path from `ask` and `ontos
query`, but the MCP `search` / `traverse` / `explain` / `provenance` tools
and `ontos export` read the store directly and still returned it. The
same fact must now be excluded everywhere, must not bridge traversals or
exports, and must not surface through audit records or warnings.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from fastmcp import Client
from typer.testing import CliRunner

import ontos.llm
import ontos.planner
from ontos.audit.emitter import AuditEmitter
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.cli.main import app
from ontos.migration.records import AddPatternChange, MigrationRecord, load_migration_record
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact, Provenance
from ontos.runtime.ontology_approval import _audit_approval
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fact(subject: str, predicate: str, obj: str, version: str, *, day: int) -> Fact:
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="doc.txt",
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.test",
            ontology_version=version,
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=_T0,
        ingested_at=_T0 + timedelta(days=day),
    )


# Active ontology is 0.2, with one verified edge 0.1 -> 0.2.
CURRENT = _fact("alice", "works_at", "acme", "0.2", day=2)
VERIFIED = _fact("acme", "works_at", "widget", "0.1", day=3)
# No path from 0.0, and no version at all: both excluded. EXCLUDED is the
# oldest, so it ranks first in search ties and is the only route to SECRET.
EXCLUDED = _fact("alice", "works_at", "bridge", "0.0", day=0)
UNSTAMPED = _fact("alice", "works_at", "legacy", "", day=1)
# Valid on its own, but reachable from alice only through EXCLUDED.
BEYOND = _fact("bridge", "works_at", "secret-co", "0.2", day=4)
HIDDEN = [EXCLUDED, UNSTAMPED]
ALL = [CURRENT, VERIFIED, EXCLUDED, UNSTAMPED, BEYOND]


def _migration_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    for name in ("ONTOS_MIGRATIONS_DIR", "ONTOS_ONTOLOGY_HISTORY_DIR", "ONTOS_ONTOLOGY_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONTOS_AUTHZ_BACKEND", "none")
    history, migrations = tmp_path / "history", tmp_path / "migrations"
    history.mkdir()
    migrations.mkdir()
    base = Ontology(
        id="ontos.test",
        version="0.1",
        entity_types=[EntityType(label="Person"), EntityType(label="Company")],
        relation_types=[RelationType(label="works_at"), RelationType(label="acquired")],
        patterns=[Pattern(subject_type="Person", predicate="works_at", object_type="Company")],
    )
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
    active = tmp_path / "active.yaml"
    write_ontology_version(
        base=base, record=record, ontology_path=active, migration_path=migrations / "m.yaml"
    )
    return replace(
        RuntimeConfig.from_env(),
        ontology_path=active,
        migration_dir=migrations,
        ontology_history_dir=history,
    )


async def _approved_server_audit(
    cfg: RuntimeConfig,
    signing_key: bytes,
) -> AuditEmitter:
    """Create a real approval for the pinned migration fixture."""
    assert cfg.migration_dir is not None
    migration_path = cfg.migration_dir / "m.yaml"
    record = load_migration_record(migration_path)

    assert len(record.approved_changes) == 1
    change = record.approved_changes[0]
    assert isinstance(change, AddPatternChange)

    audit = AuditEmitter(signing_key=signing_key)
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

    approved_record = record.model_copy(update={"approval_query_id": approval.query_id})
    migration_path.write_text(
        yaml.safe_dump(approved_record.model_dump(mode="json")),
        encoding="utf-8",
    )

    assert audit.verify_chain()
    return audit


async def _store() -> NetworkxStore:
    store = NetworkxStore()
    for fact in ALL:
        await store.add_fact(fact)
    return store


def _ids(result: Any) -> set[str]:
    return {item["id"] for item in result.data.payload}


def _no_hidden_ids(text: str) -> None:
    for fact in HIDDEN:
        assert str(fact.id) not in text


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    return _migration_config(tmp_path, monkeypatch)


async def test_direct_reads_exclude_facts_without_a_verified_path(cfg: RuntimeConfig) -> None:
    audit = await _approved_server_audit(cfg, b"direct-reads")
    server = build_server(cfg, store=await _store(), audit=audit)

    async with Client(server) as client:
        traversed = await client.call_tool(
            "traverse", {"start": "alice", "depth": 3, "agent_identity": "a"}
        )
        explained = await client.call_tool("explain", {"entity_id": "alice", "agent_identity": "a"})
        searched = await client.call_tool(
            "search", {"query": "works_at", "k": 10, "agent_identity": "a"}
        )
        verified = await client.call_tool(
            "provenance", {"fact_id": str(VERIFIED.id), "agent_identity": "a"}
        )
        excluded = await client.call_tool(
            "provenance", {"fact_id": str(EXCLUDED.id), "agent_identity": "a"}
        )

    # The verified historical fact stays; BEYOND is reachable only through
    # EXCLUDED, so traversal must not bridge to it.
    assert _ids(traversed) == {str(CURRENT.id), str(VERIFIED.id)}
    assert _ids(explained) == {str(CURRENT.id)}
    assert _ids(searched) == {str(CURRENT.id), str(VERIFIED.id), str(BEYOND.id)}
    assert verified.data.payload["id"] == str(VERIFIED.id)
    # Excluded reads exactly like a nonexistent fact.
    assert excluded.data.payload is None

    for record in audit._chain:
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_approve":
            continue
        # The provenance call's query_text is the id the caller supplied;
        # what the server reports back must never include an excluded fact.
        _no_hidden_ids(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
        _no_hidden_ids(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        assert decisions[-1] == {
            "policy": "ontology_migration",
            "ontology_id": "ontos.test",
            "ontology_version": "0.2",
        }


async def test_search_still_returns_k_when_the_top_hit_is_excluded(cfg: RuntimeConfig) -> None:
    audit = await _approved_server_audit(cfg, b"k")
    server = build_server(cfg, store=await _store(), audit=audit)

    async with Client(server) as client:
        result = await client.call_tool(
            "search", {"query": "works_at", "k": 1, "agent_identity": "a"}
        )

    # EXCLUDED and UNSTAMPED are the two oldest ties; the refetch skips them.
    assert _ids(result) == {str(CURRENT.id)}


async def test_ask_and_direct_reads_agree(
    cfg: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _StubLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _Planner:
        id = "stub"

        def __init__(self, llm: object, ontology: Ontology) -> None:
            pass

        async def plan(self, question: str, ontology: Ontology) -> Plan:
            return Plan.build(
                seed=SeedByEntity(entity_id="alice"),
                steps=[TraversalStep(depth=3, direction="out")],
                ontology=ontology,
            )

    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _Planner)
    audit = await _approved_server_audit(cfg, b"agree")
    server = build_server(
        replace(cfg, planner_backend="ollama"),
        store=await _store(),
        audit=audit,
    )

    async with Client(server) as client:
        asked = await client.call_tool("ask", {"question": "?", "agent_identity": "a"})
        traversed = await client.call_tool(
            "traverse", {"start": "alice", "depth": 3, "agent_identity": "a"}
        )

    hits = {hit["fact"]["id"] for hit in asked.data.payload["hits"]}
    assert hits == _ids(traversed) == {str(CURRENT.id), str(VERIFIED.id)}


async def test_without_migrations_reads_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(
        _migration_config(tmp_path, monkeypatch), migration_dir=None, ontology_history_dir=None
    )
    server = build_server(cfg, store=await _store(), audit=AuditEmitter(signing_key=b"legacy"))

    async with Client(server) as client:
        traversed = await client.call_tool(
            "traverse", {"start": "alice", "depth": 3, "agent_identity": "a"}
        )

    assert _ids(traversed) == {str(f.id) for f in ALL}


async def _persist_export_approval(
    cfg: RuntimeConfig,
    audit_url: str,
) -> None:
    """Persist a matching Article-12 approval for CLI export tests."""
    assert cfg.migration_dir is not None
    migration_path = cfg.migration_dir / "m.yaml"
    record = load_migration_record(migration_path)

    assert len(record.approved_changes) == 1
    change = record.approved_changes[0]
    assert isinstance(change, AddPatternChange)

    audit = PostgresAuditEmitter.from_url(
        audit_url,
        signing_key_env="ONTOS_AUDIT_SIGNING_KEY",
    )
    try:
        await audit.initialize()

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

        assert await audit.verify_chain_async()
    finally:
        await audit.close()

    approved_record = record.model_copy(update={"approval_query_id": approval.query_id})
    migration_path.write_text(
        yaml.safe_dump(approved_record.model_dump(mode="json")),
        encoding="utf-8",
    )


def _export(
    cfg: RuntimeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> str:
    store_path = tmp_path / "store.pkl"

    async def seed() -> None:
        store = NetworkxStore(path=store_path)
        for fact in ALL:
            await store.add_fact(fact)
        await store.close()

    asyncio.run(seed())
    assert cfg.migration_dir is not None and cfg.ontology_history_dir is not None
    assert cfg.ontology_path is not None
    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", str(cfg.migration_dir))
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", str(cfg.ontology_history_dir))
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(cfg.ontology_path))
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "export-migration-key")

    # Use the same persistent audit history that export verifies.
    audit_url = f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}"
    asyncio.run(_persist_export_approval(cfg, audit_url))

    output = tmp_path / "graph.graphml"
    result = CliRunner().invoke(
        app,
        ["export", "graphml", "--storage-path", str(store_path), "--output", str(output), *extra],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return output.read_text(encoding="utf-8")


def test_export_excludes_facts_and_their_raw_endpoints(
    cfg: RuntimeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(cfg, tmp_path, monkeypatch)

    assert str(CURRENT.id) in exported
    assert str(VERIFIED.id) in exported
    assert str(BEYOND.id) in exported
    _no_hidden_ids(exported)
    # An untyped node named only by an excluded fact goes with it.
    assert '"legacy"' not in exported


def test_neighbourhood_export_is_not_bridged_by_excluded_facts(
    cfg: RuntimeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(cfg, tmp_path, monkeypatch, "--entity", "alice", "--hops", "3")

    assert str(CURRENT.id) in exported
    assert str(VERIFIED.id) in exported
    _no_hidden_ids(exported)
    assert str(BEYOND.id) not in exported
    for raw_node in ("bridge", "secret-co", "legacy"):
        assert f'"{raw_node}"' not in exported
