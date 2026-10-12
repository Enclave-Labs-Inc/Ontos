"""EU AI Act Article 12: every AI-influenced decision produces an audit record
with the 12 required fields; the chain is tamper-evident."""

from __future__ import annotations

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.runtime.models import ArticleTwelveField


def test_emitter_refuses_empty_key() -> None:
    with pytest.raises(ValueError):
        AuditEmitter(signing_key=b"")


def test_emit_populates_all_twelve_fields(audit: AuditEmitter) -> None:
    record = audit.emit(
        query_id=None,
        agent_identity="claude-code:session-abc",
        acting_on_behalf_of="user:alice",
        query_text="Which 10-K filings mention supply chain risk?",
        tool_invoked="search",
        tool_arguments={"k": 5},
        result_fact_ids=["fact-1", "fact-2"],
        latency_ms=42.5,
        model_versions={"runtime": "0.0.1"},
        policy_decisions=[{"policy": "openfga", "decision": "allow"}],
    )
    # Every Article-12 field must be present.
    for field in ArticleTwelveField:
        assert field in record.article12, f"Article-12 field missing: {field.value}"
        assert record.article12[field] != "" or field == ArticleTwelveField.ACTING_ON_BEHALF_OF


def test_chain_is_hash_linked(audit: AuditEmitter) -> None:
    r1 = audit.emit(
        query_id=None,
        agent_identity="a",
        acting_on_behalf_of=None,
        query_text="q1",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=[],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    r2 = audit.emit(
        query_id=None,
        agent_identity="a",
        acting_on_behalf_of=None,
        query_text="q2",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=[],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    assert r1.prev_hash is None
    assert r2.prev_hash == r1.hash


def test_verify_chain_passes_for_untampered(audit: AuditEmitter) -> None:
    for i in range(5):
        audit.emit(
            query_id=None,
            agent_identity="a",
            acting_on_behalf_of=None,
            query_text=f"q{i}",
            tool_invoked="search",
            tool_arguments={},
            result_fact_ids=[],
            latency_ms=1.0,
            model_versions={},
            policy_decisions=[],
        )
    assert audit.verify_chain()


def test_passed_in_audit_emitter_is_not_replaced(audit: AuditEmitter) -> None:
    """Regression: `AuditEmitter.__len__` makes an empty emitter falsy, which
    would silently drop a caller-supplied emitter in `build_server` if we used
    truthiness rather than `is None`. If this ever falls back to `from_env`, the
    audit chain a caller carefully constructed is lost — that is unacceptable
    in a compliance path."""
    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import build_server
    from ontos.storage.networkx_store import NetworkxStore

    assert len(audit) == 0  # empty emitter — falsy by __len__
    assert bool(audit) is False
    cfg = RuntimeConfig.from_env()
    server = build_server(cfg, store=NetworkxStore(), audit=audit)
    assert server is not None  # would raise if from_env was called without env var


def test_by_query_id_retrieves_specific_record(audit: AuditEmitter) -> None:
    target = audit.emit(
        query_id=None,
        agent_identity="a",
        acting_on_behalf_of=None,
        query_text="find me later",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=["needle"],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    got = audit.by_query_id(target.query_id)
    assert got is not None
    assert got.hash == target.hash


async def test_storage_credentials_never_enter_audit_payload_or_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#37: graph-storage credentials must never leak into audit/log output."""
    import dataclasses
    import json

    from fastmcp import Client

    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import build_server
    from ontos.storage.networkx_store import NetworkxStore

    secret = "issue37-audit-super-secret"

    cfg = dataclasses.replace(
        RuntimeConfig.from_env(),
        storage_backend="neo4j",
        storage_path=None,
        neo4j_uri="neo4j://db.example:7687",
        neo4j_user="alice",
        neo4j_password=secret,
        neo4j_database="knowledge",
    )

    audit = AuditEmitter(signing_key=b"issue37-test-key")

    # Explicit store avoids needing a live Neo4j instance; the purpose of
    # this compliance test is the runtime audit/log path, not connectivity.
    server = build_server(
        cfg,
        store=NetworkxStore(),
        audit=audit,
    )

    async with Client(server) as client:
        await client.call_tool(
            "search",
            {
                "query": "anything",
                "agent_identity": "compliance:issue37",
            },
        )

    assert len(audit) == 1
    record = audit._chain[-1]

    audit_payload = json.dumps(
        {field.value: value for field, value in record.article12.items()},
        sort_keys=True,
    )

    assert secret not in audit_payload
    assert "db.example:7687" not in audit_payload
    assert "alice" not in audit_payload

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err

    assert secret not in combined_output
    assert "db.example:7687" not in combined_output


@pytest.mark.parametrize("tool", ["search", "traverse", "explain"])
async def test_offsetless_as_of_is_answered_and_audited(audit: AuditEmitter, tool: str) -> None:
    """#60: an `as_of` without a UTC offset used to raise TypeError inside the
    store, before `_emit`, so the request left no Article-12 record."""
    import dataclasses
    from datetime import UTC, datetime

    from fastmcp import Client

    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.runtime.server import build_server
    from ontos.storage.networkx_store import NetworkxStore

    store = NetworkxStore()
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await store.add_fact(fact)

    cfg = dataclasses.replace(RuntimeConfig.from_env(), storage_path=None)
    server = build_server(cfg, store=store, audit=audit)

    arguments: dict[str, object] = {"agent_identity": "compliance:issue60", "as_of": "2026-03-01"}
    if tool == "search":
        arguments["query"] = "alice"
    elif tool == "traverse":
        arguments["start"] = "person:alice"
    else:
        arguments["entity_id"] = "person:alice"

    async with Client(server) as client:
        await client.call_tool(tool, arguments)

    assert len(audit) == 1
    record = audit._chain[-1]
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == tool
