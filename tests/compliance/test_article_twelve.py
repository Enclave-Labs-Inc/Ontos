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
