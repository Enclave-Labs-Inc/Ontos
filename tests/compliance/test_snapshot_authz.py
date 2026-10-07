"""#33 — Snapshot-serve authz isolation.

Regression gates from the PR #53 review (BLOCKING #2 + SUGGESTION #4):

- `snapshot_info` must not reveal the number of facts in the frozen
  graph. Pre-review the tool returned `fact_count = len(store._facts)`,
  an unfiltered total — any caller learned how many facts exist
  including ones their `allowed_acls` forbid. CLAUDE.md: "Never leak
  the *existence* of a forbidden node (no counts, no 'hidden'
  markers)."

- `snapshot_info` must not reveal the server's filesystem layout. Pre-
  review the tool returned the absolute path of the pickle (`.resolve()`).
  Now returns a 16-hex-char content hash, which carries no path or
  naming-scheme information.

- `snapshot_info` returns identical bytes to a restricted caller and
  an unrestricted caller — the response is pure load-time metadata,
  captured once pre-ACL, cached frozen on `SnapshotMetadata`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.cli.main import _collect_snapshot_metadata
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore


def _unwrap(call_result: Any) -> dict[str, Any]:
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
def mixed_acl_snapshot(tmp_path: Path) -> Path:
    """Build a snapshot with facts under three ACLs: public, finance,
    hr. A caller whose allowed_acls=['finance'] sees fewer facts than
    the global total; the pre-review `fact_count` would have leaked
    the total to that caller."""
    path = tmp_path / "mixed.pkl"

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
        pairs = [
            ("person:alice", "knows", "person:bob", None),
            ("person:alice", "works_at", "company:acme", "finance"),
            ("person:alice", "manages", "person:carol", "finance"),
            ("person:alice", "reports_to", "person:eve", "hr"),
            ("person:alice", "mentors", "person:dan", "hr"),
        ]
        for i, (s, p, o, acl) in enumerate(pairs):
            await store.add_fact(
                Fact(
                    subject_id=s,
                    predicate=p,
                    object_id=o,
                    provenance=prov,
                    t_valid=now - timedelta(days=10 - i),
                    ingested_at=now - timedelta(days=10 - i),
                    acl_ref=acl,
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
def snapshot_server(mixed_acl_snapshot: Path, audit: AuditEmitter):
    store = NetworkxStore(path=mixed_acl_snapshot)
    store._path = None
    metadata = _collect_snapshot_metadata(mixed_acl_snapshot, store)
    cfg = RuntimeConfig.from_env()
    return build_server(cfg, store=store, audit=audit, snapshot_metadata=metadata)


async def test_snapshot_info_payload_does_not_leak_fact_count(
    mixed_acl_snapshot: Path, snapshot_server
) -> None:
    """A caller whose `allowed_acls` forbids the finance and hr facts
    would, pre-review, have learned the global total of 5 via the
    returned `fact_count`. The new metadata carries no such field —
    the regression gate is a snapshot-level string check so no future
    refactor can silently re-introduce it."""
    async with Client(snapshot_server) as client:
        result = await client.call_tool("snapshot_info", {"agent_identity": "restricted:caller"})
    payload = _unwrap(result)["payload"]
    serialized = json.dumps(payload)
    assert "fact_count" not in serialized
    # Belt-and-braces: the actual number of facts in the store (5)
    # must not appear anywhere in the payload. If a future field
    # happens to include it, this gate fires.
    assert '"5"' not in serialized
    assert ": 5" not in serialized


async def test_snapshot_info_payload_does_not_leak_filesystem_path(
    mixed_acl_snapshot: Path, snapshot_server
) -> None:
    """The absolute path (`/private/var/.../mixed.pkl`) was the first
    draft's `snapshot_path`. Agents don't need it; now the id is a
    content hash with no path or filename component."""
    async with Client(snapshot_server) as client:
        result = await client.call_tool("snapshot_info", {"agent_identity": "restricted:caller"})
    payload = _unwrap(result)["payload"]
    serialized = json.dumps(payload)
    # No field named like a path leak.
    assert "snapshot_path" not in payload
    assert "path" not in payload
    # The actual filesystem path doesn't appear in any field.
    for component in (
        str(mixed_acl_snapshot.resolve()),
        str(mixed_acl_snapshot.resolve().parent),
        mixed_acl_snapshot.name,
    ):
        assert component not in serialized, f"snapshot_info leaked path component {component!r}"


async def test_snapshot_info_returns_identical_payload_to_every_caller(
    snapshot_server,
) -> None:
    """`SnapshotMetadata` is captured at load time, pre-ACL. A
    restricted caller and an unrestricted caller must see byte-identical
    metadata — the response is not a per-caller projection onto the
    graph."""
    async with Client(snapshot_server) as client:
        unrestricted = _unwrap(
            await client.call_tool("snapshot_info", {"agent_identity": "ops:admin"})
        )["payload"]
        restricted = _unwrap(
            await client.call_tool("snapshot_info", {"agent_identity": "restricted:caller"})
        )["payload"]
    # Query ids differ by design (one per call); the metadata itself
    # must not.
    assert unrestricted == restricted


async def test_snapshot_info_call_lands_in_article_twelve_audit_chain(
    snapshot_server, audit: AuditEmitter
) -> None:
    """Pre-review the tool bypassed audit entirely. Every snapshot_info
    call now emits an Article-12 record naming the caller, with every
    required field populated."""
    from ontos.runtime.models import ArticleTwelveField

    async with Client(snapshot_server) as client:
        result = await client.call_tool("snapshot_info", {"agent_identity": "compliance:officer"})
    payload = _unwrap(result)
    record = audit.by_query_id(UUID(payload["query_id"]))
    assert record is not None
    for field in ArticleTwelveField:
        assert field in record.article12, f"Article-12 field missing: {field.value}"
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "snapshot_info"
    assert record.article12[ArticleTwelveField.AGENT_IDENTITY] == "compliance:officer"
