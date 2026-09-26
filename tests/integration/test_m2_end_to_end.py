"""M2.c — end-to-end integration test wiring authz + persistent audit into the MCP surface.

Extends M1.e with:
- InMemoryAuthz seeded per test (real OpenFGA in CI is deferred; the
  Protocol conformance already covers the drop-in).
- Runtime configured through the MCP client using both new subsystems
  at once — the test proves the composition works, not just each part
  in isolation.

Skipped by default (marker `integration`); CI runs it explicitly.
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
from ontos.authz import VIEW, InMemoryAuthz
from ontos.extraction import ExtractionInput, LlmExtractor, RawTriple
from ontos.ontology import Ontology, load_ontology
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.server import build_server
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
)


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


class ScriptedLLM:
    id: str = "scripted-llm"
    version: str = "m2e-1"

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
    return AuditEmitter(signing_key=b"m2e-test-key")


def _triple(
    subject_id: str, subject_type: str, subject_name: str,
    predicate: str,
    object_id: str, object_type: str, object_name: str,
) -> RawTriple:
    return RawTriple(
        subject_id=subject_id,
        subject_type=subject_type,
        subject_canonical_name=subject_name,
        predicate=predicate,
        object_id=object_id,
        object_type=object_type,
        object_canonical_name=object_name,
        llm_confidence=0.95,
    )


CORPUS = {
    "Alice at Acme.": [
        _triple(
            "person:alice", "Person", "Alice Smith",
            "works_at",
            "company:acme", "Company", "Acme Corp",
        )
    ],
    "Acme acquired Widget.": [
        _triple(
            "company:acme", "Company", "Acme Corp",
            "acquired",
            "company:widget", "Company", "Widget Inc",
        )
    ],
}


async def _ingest(
    extractor: LlmExtractor,
    store: Neo4jStore,
    texts: dict[str, list[RawTriple]],
    *,
    acl_ref: str | None = None,
) -> None:
    for i, text in enumerate(texts):
        result = await extractor.extract(
            ExtractionInput(source_id=f"doc-{i}-{acl_ref or 'pub'}", text=text)
        )
        for fact in result.facts:
            if acl_ref is not None:
                fact = fact.model_copy(update={"acl_ref": acl_ref})
            await store.add_fact(fact)


async def test_authz_backend_wired_end_to_end(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """Alice's grants let her see hr-tagged facts; Bob (no grants) doesn't."""
    public_docs = dict(list(CORPUS.items())[:1])
    private_docs = dict(list(CORPUS.items())[1:])

    await _ingest(LlmExtractor(ScriptedLLM(public_docs), ontology), store, public_docs)
    await _ingest(
        LlmExtractor(ScriptedLLM(private_docs), ontology),
        store,
        private_docs,
        acl_ref="acl:hr",
    )

    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:hr")
    # Bob has no grants.

    server: FastMCP = build_server(store=store, audit=audit, authz=authz)

    async with Client(server) as client:
        alice_result = await client.call_tool(
            "search",
            {
                "query": "acme",
                "agent_identity": "agent:x",
                "acting_on_behalf_of": "user:alice",
            },
        )
        bob_result = await client.call_tool(
            "search",
            {
                "query": "acme",
                "agent_identity": "agent:x",
                "acting_on_behalf_of": "user:bob",
            },
        )

    alice_facts = _unwrap(alice_result)["payload"]
    bob_facts = _unwrap(bob_result)["payload"]

    # Alice sees both. Bob only sees the public one.
    assert len(alice_facts) == 2
    assert len(bob_facts) == 1
    for fact in bob_facts:
        assert fact["acl_ref"] is None


async def test_policy_decisions_recorded_in_audit_when_authz_is_wired(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """Every request that asks the authz backend must leave a policy_decisions
    breadcrumb in the audit record — that's the compliance evidence trail."""
    public_docs = dict(list(CORPUS.items())[:1])
    await _ingest(LlmExtractor(ScriptedLLM(public_docs), ontology), store, public_docs)

    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:hr")
    server = build_server(store=store, audit=audit, authz=authz)

    async with Client(server) as client:
        result = await client.call_tool(
            "search",
            {
                "query": "acme",
                "agent_identity": "agent:x",
                "acting_on_behalf_of": "user:alice",
            },
        )

    from uuid import UUID as _UUID

    payload = _unwrap(result)
    record = audit.by_query_id(_UUID(payload["query_id"]))
    assert record is not None
    policy_decisions_json = record.article12[ArticleTwelveField.POLICY_DECISIONS]
    decisions = json.loads(policy_decisions_json)
    assert len(decisions) == 1
    assert decisions[0]["policy"] == "inmemory-authz"
    assert decisions[0]["subject"] == "user:alice"
    assert decisions[0]["relation"] == VIEW
    assert "acl:hr" in decisions[0]["result"]


async def test_no_authz_backend_preserves_m1_shim(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """With no authz configured, the M1 acl_subject==acl_ref shim still works —
    ensures we haven't broken the dev path in the M2 refactor."""
    public_docs = dict(list(CORPUS.items())[:1])
    private_docs = dict(list(CORPUS.items())[1:])
    await _ingest(LlmExtractor(ScriptedLLM(public_docs), ontology), store, public_docs)
    await _ingest(
        LlmExtractor(ScriptedLLM(private_docs), ontology),
        store,
        private_docs,
        acl_ref="acl:hr",
    )

    server = build_server(store=store, audit=audit, authz=None)

    async with Client(server) as client:
        # acting_on_behalf_of="acl:hr" is the M1 shim match — dev-mode caller.
        result = await client.call_tool(
            "search",
            {
                "query": "acme",
                "agent_identity": "agent:x",
                "acting_on_behalf_of": "acl:hr",
            },
        )

    facts = _unwrap(result)["payload"]
    assert len(facts) == 2  # public + hr, via shim


async def test_persistent_audit_survives_new_emitter_instance(
    postgres_container,
) -> None:
    """Emit via one PostgresAuditEmitter, then read via a fresh instance —
    proves data actually persisted, not just held in-process."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from ontos.audit.postgres import PostgresAuditEmitter

    url = postgres_container.get_connection_url()
    e1 = PostgresAuditEmitter(
        engine=create_async_engine(url, future=True),
        signing_key=b"m2c-key",
    )
    await e1.initialize()

    from ontos.audit.postgres import _audit_table

    async with e1._engine.begin() as conn:
        await conn.execute(_audit_table.delete())

    original = await e1.emit_async(
        query_id=None,
        agent_identity="agent:persist",
        acting_on_behalf_of=None,
        query_text="persist me",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=[],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    await e1.close()

    e2 = PostgresAuditEmitter(
        engine=create_async_engine(url, future=True),
        signing_key=b"m2c-key",
    )
    try:
        got = await e2.by_query_id_async(original.query_id)
        assert got is not None
        assert got.hash == original.hash
        assert await e2.verify_chain_async()
    finally:
        await e2.close()


@pytest.fixture(scope="module")
def postgres_container():
    docker = pytest.importorskip("docker")
    testcontainers_postgres = pytest.importorskip("testcontainers.postgres")
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker not reachable: {exc}")

    container = testcontainers_postgres.PostgresContainer(
        image="postgres:16", driver="asyncpg"
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


async def test_authz_and_bitemporal_compose(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """The four features that make M2 shippable interact correctly."""
    public_docs = dict(list(CORPUS.items())[:1])
    await _ingest(LlmExtractor(ScriptedLLM(public_docs), ontology), store, public_docs)

    # Close the fact so it's historical.
    hits = await store.search("acme")
    assert len(hits) == 1
    now = datetime.now(UTC)
    await store.close_fact(hits[0].id, t_invalid=now)

    authz = InMemoryAuthz()
    server = build_server(store=store, audit=audit, authz=authz)

    async with Client(server) as client:
        current = await client.call_tool(
            "search",
            {"query": "acme", "agent_identity": "agent:x", "acting_on_behalf_of": "user:x"},
        )
        historical = await client.call_tool(
            "search",
            {
                "query": "acme",
                "agent_identity": "agent:x",
                "acting_on_behalf_of": "user:x",
                "as_of": hits[0].ingested_at.isoformat(),
            },
        )

    assert _unwrap(current)["payload"] == []
    assert len(_unwrap(historical)["payload"]) == 1
