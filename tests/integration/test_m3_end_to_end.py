"""M3.c — end-to-end: NL question → planner → executor → ranked results.

Boots the FastMCP surface with a real Neo4j (Testcontainers), a
deterministic Planner backed by a FakeLLM, and the M3.b executor.
Asserts the full ask() pipeline works end-to-end with provenance
intact and Article-12 audit records populated with the plan JSON.

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
from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import (
    LlmPlanner,
    RawPlan,
    SeedByEntity,
    SeedByKeyword,
    TraversalStep,
)
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact, Provenance
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


class ScriptedPlannerLLM:
    """Deterministic planner-LLM backend.

    Maps input question → RawPlan; the LlmPlanner from M3.a then
    validates against the ontology and hands the executor a Plan.
    """

    id: str = "scripted-planner"
    version: str = "m3e-1"

    def __init__(self, plans: dict[str, RawPlan]) -> None:
        self._plans = plans

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan:
        if question not in self._plans:
            raise KeyError(f"scripted planner has no plan for {question!r}")
        return self._plans[question]


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
    return AuditEmitter(signing_key=b"m3e-test-key")


def _fact(subject: str, predicate: str, obj: str) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="m3e-test",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def _seed_graph(store: Neo4jStore) -> None:
    """Chain: alice --works_at→ acme --acquired→ widget --subsidiary_of→ foo."""
    await store.add_fact(_fact("person:alice", "works_at", "company:acme"))
    await store.add_fact(_fact("company:acme", "acquired", "company:widget"))
    await store.add_fact(_fact("company:widget", "subsidiary_of", "company:foo"))


async def test_ask_end_to_end_returns_ranked_hits_with_provenance(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter, monkeypatch
) -> None:
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(STARTER_PATH))
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")  # cfg only; store passed explicitly

    await _seed_graph(store)

    plans = {
        "What does Alice do at Acme?": RawPlan(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[TraversalStep(depth=3)],
            limit=10,
        )
    }
    planner = LlmPlanner(ScriptedPlannerLLM(plans), ontology)
    server: FastMCP = build_server(
        RuntimeConfig.from_env(),
        store=store,
        audit=audit,
        planner=planner,
        executor=DeterministicExecutor(),
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "ask",
            {
                "question": "What does Alice do at Acme?",
                "agent_identity": "agent:m3e",
                "acting_on_behalf_of": "user:m3e",
            },
        )

    payload = _unwrap(result)["payload"]
    assert "hits" in payload
    hits = payload["hits"]
    assert len(hits) >= 1
    # Ranked descending by combined_score
    scores = [h["combined_score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    # Provenance intact on every hit's underlying fact
    for h in hits:
        prov = h["fact"]["provenance"]
        assert prov["source_id"] == "m3e-test"
        assert prov["extractor_id"] == "test"


async def test_ask_writes_plan_json_into_article_twelve_audit(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter, monkeypatch
) -> None:
    """Compliance guarantee: the audit record captures the exact plan
    the planner emitted. Regulators reviewing an answer can reproduce
    exactly what the runtime asked for."""
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(STARTER_PATH))

    await _seed_graph(store)

    plans = {
        "acme?": RawPlan(
            seed=SeedByKeyword(keyword="acme", k=3),
            steps=[TraversalStep(depth=2)],
            limit=5,
        )
    }
    planner = LlmPlanner(ScriptedPlannerLLM(plans), ontology)
    server = build_server(
        RuntimeConfig.from_env(),
        store=store,
        audit=audit,
        planner=planner,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "ask",
            {"question": "acme?", "agent_identity": "agent:x"},
        )

    from uuid import UUID as _UUID

    payload = _unwrap(result)
    record = audit.by_query_id(_UUID(payload["query_id"]))
    assert record is not None
    args_json = record.article12[ArticleTwelveField.TOOL_ARGUMENTS]
    args = json.loads(args_json)
    assert "plan" in args
    assert args["plan"]["seed"]["kind"] == "by_keyword"
    assert args["plan"]["seed"]["keyword"] == "acme"
    assert args["planner_id"].startswith("ontos.llm_planner+")
    assert args["executor_id"].startswith("ontos.executor.deterministic")


async def test_ask_with_no_planner_returns_clear_error(
    store: Neo4jStore, audit: AuditEmitter, monkeypatch
) -> None:
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(STARTER_PATH))
    await _seed_graph(store)

    server = build_server(
        RuntimeConfig.from_env(), store=store, audit=audit
    )  # no planner

    async with Client(server) as client:
        result = await client.call_tool(
            "ask", {"question": "anything", "agent_identity": "agent:x"}
        )

    payload = _unwrap(result)["payload"]
    assert "error" in payload
    assert "planner" in payload["error"].lower()


async def test_ask_with_no_ontology_returns_clear_error(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter
) -> None:
    """Even with a planner, running ask() without an ontology on the runtime
    is a hard fail — the planner can't validate without one."""
    planner = LlmPlanner(ScriptedPlannerLLM({}), ontology)
    server = build_server(
        RuntimeConfig.from_env(), store=store, audit=audit, planner=planner
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "ask", {"question": "anything", "agent_identity": "agent:x"}
        )

    payload = _unwrap(result)["payload"]
    assert "error" in payload
    assert "ontology" in payload["error"].lower()


async def test_planner_error_surfaces_via_ask_without_crashing(
    store: Neo4jStore, ontology: Ontology, audit: AuditEmitter, monkeypatch
) -> None:
    """When the planner-LLM raises, the ask tool returns a structured error
    with an audit record — not a 500 to the agent."""
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(STARTER_PATH))
    planner = LlmPlanner(
        ScriptedPlannerLLM({}),  # KeyError on any question
        ontology,
    )
    server = build_server(
        RuntimeConfig.from_env(), store=store, audit=audit, planner=planner
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "ask", {"question": "unmapped question", "agent_identity": "agent:x"}
        )

    payload = _unwrap(result)
    assert "error" in payload["payload"]
    assert payload["audit_hash"]  # audit was still written
