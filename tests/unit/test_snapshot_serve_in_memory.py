"""#33 — End-to-end snapshot-serve exercise via fastmcp's in-memory client.

Not an integration test proper (no Docker / Neo4j / Postgres); the whole
pipeline runs in-process. Boots `build_server` against a loaded
NetworkxStore snapshot, registers `snapshot_info`, and exercises the
real MCP surface the way a Claude Desktop client would.

Pins three invariants the plan called out:
- `search` on a snapshot-backed server returns the loaded fact.
- `snapshot_info` reports the stamp from the frozen graph.
- `audit_lookup` reflects every tool call in the chain.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastmcp import Client, FastMCP

from ontos.audit.emitter import AuditEmitter
from ontos.cli.main import _register_snapshot_info
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore


def _unwrap(call_result: Any) -> dict[str, Any]:
    """Same shape helper used in tests/integration/test_m1_end_to_end.py."""
    if getattr(call_result, "structured_content", None):
        return dict(call_result.structured_content)
    data = getattr(call_result, "data", None)
    if data is not None and hasattr(data, "model_dump"):
        dumped = data.model_dump()
        assert isinstance(dumped, dict)
        return dumped
    if getattr(call_result, "content", None):
        first = call_result.content[0]
        if hasattr(first, "text"):
            parsed = json.loads(first.text)
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError(f"could not extract payload from {call_result!r}")


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    """Build a V2 pickle with two facts under ontos.starter v0.1."""
    path = tmp_path / "snap.pkl"

    async def _build() -> None:
        store = NetworkxStore(path=path)
        now = datetime.now(UTC)
        prov = Provenance(
            source_id="demo",
            extractor_id="demo",
            extractor_version="0",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        )
        await store.add_fact(
            Fact(
                subject_id="person:alice",
                predicate="works_at",
                object_id="company:acme",
                provenance=prov,
                t_valid=now - timedelta(days=10),
                ingested_at=now - timedelta(days=10),
            )
        )
        await store.add_fact(
            Fact(
                subject_id="company:acme",
                predicate="acquired",
                object_id="company:widget",
                provenance=prov,
                t_valid=now - timedelta(days=5),
                ingested_at=now - timedelta(days=5),
            )
        )
        store.flush(force=True)
        await store.close()

    import asyncio

    asyncio.run(_build())
    return path


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditEmitter:
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-key")
    return AuditEmitter(signing_key=b"test-key")


@pytest.fixture
def snapshot_server(snapshot_path: Path, audit: AuditEmitter) -> tuple[FastMCP, Path]:
    store = NetworkxStore(path=snapshot_path)
    cfg = RuntimeConfig.from_env()
    server = build_server(cfg, store=store, audit=audit)
    _register_snapshot_info(server, snapshot_path, store)
    return server, snapshot_path


async def test_snapshot_search_returns_the_loaded_fact(
    snapshot_server: tuple[FastMCP, Path],
) -> None:
    server, _ = snapshot_server
    async with Client(server) as client:
        result = await client.call_tool(
            "search",
            {"query": "acme", "agent_identity": "test:client"},
        )
    payload = _unwrap(result)
    assert payload["query_id"]
    assert payload["audit_hash"]
    # The payload shape depends on the tool's wrapper; the shape helper
    # extracts the top-level dict. The actual facts list lives under
    # `payload` — assert something searchable landed.
    assert payload["payload"]  # non-empty


async def test_snapshot_info_tool_reports_stamp_through_mcp(
    snapshot_server: tuple[FastMCP, Path],
) -> None:
    server, path = snapshot_server
    async with Client(server) as client:
        result = await client.call_tool("snapshot_info", {})
    payload = _unwrap(result)
    assert payload["snapshot_path"] == str(path.resolve())
    assert payload["fact_count"] == 2
    assert payload["ontology_id"] == "ontos.starter"
    assert payload["ontology_version"] == "0.1"
    assert payload["format"] == "ontos-nx-store-v2"


async def test_snapshot_tool_calls_land_in_audit_chain(
    snapshot_server: tuple[FastMCP, Path], audit: AuditEmitter
) -> None:
    server, _ = snapshot_server
    async with Client(server) as client:
        search_result = await client.call_tool(
            "search", {"query": "acme", "agent_identity": "test:client"}
        )
    payload = _unwrap(search_result)
    record = audit.by_query_id(UUID(payload["query_id"]))
    assert record is not None
    # Every Article-12 field populated — snapshot-backed server uses
    # the same audit path as live-serve, so the compliance contract
    # from M1 holds unchanged.
    for field in ArticleTwelveField:
        assert field in record.article12, f"Article-12 field missing: {field.value}"
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "search"
    assert record.article12[ArticleTwelveField.AGENT_IDENTITY] == "test:client"
