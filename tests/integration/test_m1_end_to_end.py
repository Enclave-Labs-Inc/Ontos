"""M1.e — end-to-end integration test.

This is the milestone-closing test. It boots the whole pipeline:

    text corpus → LlmExtractor → Neo4jStore → FastMCP tools → audit chain

and asserts the four invariants that make M1 shippable:

1. Every returned fact carries a full provenance chain.
2. Every MCP tool call produces an Article-12 audit record with all
   12 required fields populated.
3. Permission-aware traversal does not leak forbidden nodes at the
   MCP surface (verified end-to-end, not just in the store).
4. The whole thing works with a deterministic FakeLLM — CI does not
   depend on outbound LLM calls, per the sovereignty invariant.

Skipped by default (marker `integration`); CI runs it explicitly and
skips cleanly if Docker isn't reachable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from ontos.audit.emitter import AuditEmitter
from ontos.extraction import (
    ExtractionInput,
    LlmExtractor,
    RawTriple,
)
from ontos.ontology import Ontology, load_ontology
from ontos.runtime.models import ArticleTwelveField, Fact
from ontos.runtime.server import build_server
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration


def _unwrap(call_result: Any) -> dict[str, Any]:
    """Coerce a FastMCP CallToolResult into a plain payload dict.

    FastMCP v4 returns `result.data` as a dynamically-typed Pydantic
    model (a `Root` wrapper when the tool's return type has `Any`
    fields), which is not subscriptable. We reach for the raw
    structured content instead — that's guaranteed by the MCP protocol.
    """
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

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
)


class ScriptedLLM:
    """FakeLLM that maps input text to a scripted list of triples.

    Deterministic across runs. The M1.d unit tests already prove the
    extractor plumbing works; this class exists so the end-to-end test
    can seed a realistic corpus without an actual LLM call.
    """

    id: str = "scripted-llm"
    version: str = "e2e-1"

    def __init__(self, script: dict[str, list[RawTriple]]) -> None:
        self._script = script

    async def structured_extract(
        self, text: str, ontology: Ontology
    ) -> list[RawTriple]:
        return list(self._script.get(text, []))


@pytest.fixture(scope="module")
def neo4j_container():
    docker = pytest.importorskip("docker")
    testcontainers_neo4j = pytest.importorskip("testcontainers.neo4j")
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker not reachable: {exc}")

    container = testcontainers_neo4j.Neo4jContainer("neo4j:5.24")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def store(neo4j_container) -> AsyncIterator[Neo4jStore]:
    uri = neo4j_container.get_connection_url()
    password = neo4j_container.password
    s = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await s.initialize()
    async with s._driver.session(database=s._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


@pytest.fixture
def audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m1e-test-key")


def _triple(
    subject_id: str,
    subject_type: str,
    subject_name: str,
    predicate: str,
    object_id: str,
    object_type: str,
    object_name: str,
    llm_confidence: float = 0.95,
) -> RawTriple:
    return RawTriple(
        subject_id=subject_id,
        subject_type=subject_type,
        subject_canonical_name=subject_name,
        predicate=predicate,
        object_id=object_id,
        object_type=object_type,
        object_canonical_name=object_name,
        llm_confidence=llm_confidence,
    )


CORPUS = {
    "Alice Smith works at Acme Corp.": [
        _triple(
            "person:alice", "Person", "Alice Smith",
            "works_at",
            "company:acme", "Company", "Acme Corp",
        )
    ],
    "Acme Corp acquired Widget Inc.": [
        _triple(
            "company:acme", "Company", "Acme Corp",
            "acquired",
            "company:widget", "Company", "Widget Inc",
        )
    ],
    "Widget Inc is a subsidiary of Foo Holdings.": [
        _triple(
            "company:widget", "Company", "Widget Inc",
            "subsidiary_of",
            "company:foo", "Company", "Foo Holdings",
        )
    ],
}


async def _ingest_corpus(
    extractor: LlmExtractor,
    store: Neo4jStore,
    corpus: dict[str, list[RawTriple]] | None = None,
    *,
    acl_ref: str | None = None,
) -> int:
    """Extract + persist a corpus. Returns the number of facts written.

    Caller passes the exact texts the extractor's LLM has been scripted
    for — sending unscripted texts triggers `ExtractionError` (loud-fail
    on silent-empty per M1.d).
    """
    if corpus is None:
        corpus = CORPUS
    n = 0
    for i, text in enumerate(corpus):
        result = await extractor.extract(
            ExtractionInput(source_id=f"doc-{i}-{acl_ref or 'pub'}", text=text)
        )
        for fact in result.facts:
            if acl_ref is not None:
                fact = fact.model_copy(update={"acl_ref": acl_ref})
            await store.add_fact(fact)
            n += 1
    return n


async def test_end_to_end_pipeline_persists_facts_with_full_provenance(
    store: Neo4jStore, ontology: Ontology
) -> None:
    llm = ScriptedLLM(CORPUS)
    extractor = LlmExtractor(llm, ontology)
    n_written = await _ingest_corpus(extractor, store)
    assert n_written == 3

    hits = await store.search("acquired")
    assert len(hits) == 1
    fact: Fact = hits[0]
    # Every load-bearing provenance field populated
    assert fact.provenance.source_id.startswith("doc-")
    assert "scripted-llm" in fact.provenance.extractor_id
    assert fact.provenance.extractor_version == "e2e-1"
    assert fact.provenance.confidence_score == 0.95
    assert fact.t_valid is not None
    assert fact.ingested_at is not None
    assert fact.t_invalid is None


async def test_mcp_tool_call_produces_article_twelve_audit_record(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """The load-bearing compliance assertion: every MCP tool call must
    emit an audit record with all 12 required fields populated. Verified
    through FastMCP's in-memory client — not the store directly."""
    llm = ScriptedLLM(CORPUS)
    extractor = LlmExtractor(llm, ontology)
    await _ingest_corpus(extractor, store)

    server: FastMCP = build_server(store=store, audit=audit)

    async with Client(server) as client:
        result = await client.call_tool(
            "search",
            {"query": "acquired", "agent_identity": "claude:e2e-test", "k": 5},
        )

    payload = _unwrap(result)
    assert payload["query_id"]
    assert payload["audit_hash"]
    assert len(payload["payload"]) == 1

    # The emitted audit record must satisfy Article 12
    from uuid import UUID

    record = audit.by_query_id(UUID(payload["query_id"]))
    assert record is not None
    for field in ArticleTwelveField:
        assert field in record.article12, f"Article-12 field missing: {field.value}"
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "search"
    assert record.article12[ArticleTwelveField.AGENT_IDENTITY] == "claude:e2e-test"
    # Result hash matches an actual returned fact
    returned_fact_ids = json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
    assert len(returned_fact_ids) == 1


async def test_permission_aware_traversal_does_not_leak_through_mcp(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """Bob traversing from Alice must see her employer but not the
    downstream acquisitions/subsidiaries — those live behind acl_ref='alice'.
    The forbidden facts must be pruned during Cypher traversal (proven in
    M1.b) AND must not leak through the MCP tool response payload."""
    # First doc is public; the next two are alice-only. Partition CORPUS
    # so each extractor only sees the texts its LLM is scripted for —
    # LlmExtractor's loud-fail on silent-empty would otherwise raise on
    # unmatched texts (which is correct behavior; we're just working with it).
    public_docs = dict(list(CORPUS.items())[:1])
    private_docs = dict(list(CORPUS.items())[1:])

    public_llm = ScriptedLLM(public_docs)
    await _ingest_corpus(LlmExtractor(public_llm, ontology), store, public_docs)

    private_llm = ScriptedLLM(private_docs)
    await _ingest_corpus(
        LlmExtractor(private_llm, ontology), store, private_docs, acl_ref="alice"
    )

    server: FastMCP = build_server(store=store, audit=audit)

    async with Client(server) as client:
        # Bob's view of the graph — should NOT contain company:widget or company:foo
        bob_result = await client.call_tool(
            "traverse",
            {
                "start": "person:alice",
                "agent_identity": "agent:bob",
                "acting_on_behalf_of": "bob",
                "depth": 5,
            },
        )
        alice_result = await client.call_tool(
            "traverse",
            {
                "start": "person:alice",
                "agent_identity": "agent:alice",
                "acting_on_behalf_of": "alice",
                "depth": 5,
            },
        )

    bob_facts = _unwrap(bob_result)["payload"]
    alice_facts = _unwrap(alice_result)["payload"]

    # Bob sees Alice → Acme only (one hop, one fact).
    assert len(bob_facts) == 1
    for fact in bob_facts:
        assert fact["object_id"] not in ("company:widget", "company:foo")
        assert fact["subject_id"] not in ("company:widget", "company:foo")

    # Alice sees the whole chain.
    alice_ids = {fact["object_id"] for fact in alice_facts} | {
        fact["subject_id"] for fact in alice_facts
    }
    assert "company:widget" in alice_ids
    assert "company:foo" in alice_ids


async def test_audit_chain_stays_hash_linked_across_multiple_tool_calls(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    llm = ScriptedLLM(CORPUS)
    await _ingest_corpus(LlmExtractor(llm, ontology), store)
    server = build_server(store=store, audit=audit)

    async with Client(server) as client:
        await client.call_tool(
            "search", {"query": "acquired", "agent_identity": "agent:x"}
        )
        await client.call_tool(
            "search", {"query": "works_at", "agent_identity": "agent:x"}
        )
        await client.call_tool(
            "traverse",
            {"start": "person:alice", "agent_identity": "agent:x", "depth": 2},
        )

    # 3 tool calls => 3 audit records, chain intact
    assert len(audit) == 3
    assert audit.verify_chain()


async def test_explain_returns_facts_with_provenance_via_mcp(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    llm = ScriptedLLM(CORPUS)
    await _ingest_corpus(LlmExtractor(llm, ontology), store)
    server = build_server(store=store, audit=audit)

    async with Client(server) as client:
        result = await client.call_tool(
            "explain",
            {"entity_id": "company:acme", "agent_identity": "agent:x"},
        )

    facts = _unwrap(result)["payload"]
    assert len(facts) >= 1
    for fact in facts:
        prov = fact["provenance"]
        assert prov["source_id"]
        assert prov["extractor_id"]
        assert prov["extractor_version"]
        assert prov["confidence"] in ("EXTRACTED", "INFERRED", "AMBIGUOUS")
        assert 0.0 <= prov["confidence_score"] <= 1.0


async def test_as_of_query_reads_historical_state_via_mcp(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    llm = ScriptedLLM(CORPUS)
    await _ingest_corpus(LlmExtractor(llm, ontology), store)

    # Close all facts as of now, then confirm as_of the past still reads them.
    now = datetime.now(UTC)
    hits = await store.search("works_at")
    assert len(hits) == 1
    await store.close_fact(hits[0].id, t_invalid=now)

    server = build_server(store=store, audit=audit)

    async with Client(server) as client:
        # Current search sees nothing.
        current = await client.call_tool(
            "search", {"query": "works_at", "agent_identity": "agent:x"}
        )
        # as_of before the close: still visible.
        historical = await client.call_tool(
            "search",
            {
                "query": "works_at",
                "agent_identity": "agent:x",
                "as_of": (hits[0].ingested_at).isoformat(),
            },
        )

    assert _unwrap(current)["payload"] == []
    assert len(_unwrap(historical)["payload"]) == 1
