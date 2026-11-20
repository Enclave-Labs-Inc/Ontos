"""#119 phase 3: `auto` search mode, deployment default, and `ask` wiring."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastmcp import Client

import ontos.llm
import ontos.planner
from ontos.audit.emitter import AuditEmitter
from ontos.executor import DeterministicExecutor
from ontos.ontology import load_ontology
from ontos.planner import Plan, SeedByKeyword, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.migration_executor import build_verified_executor
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.lexical import resolve_search_mode
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
QUESTION_KEYWORD = "who works at Acme"


def _fact() -> Fact:
    return Fact(
        id=UUID(int=1),
        subject_id="Person:dana",
        predicate="works_at",
        object_id="Company:acme",
        provenance=Provenance(
            source_id="doc.md",
            extractor_id="test",
            extractor_version="1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=T0,
        ingested_at=T0,
    )


async def _store() -> NetworkxStore:
    store = NetworkxStore()
    await store.add_fact(_fact())
    return store


def _keyword_plan() -> Plan:
    return Plan.build(
        seed=SeedByKeyword(keyword=QUESTION_KEYWORD),
        steps=[TraversalStep(depth=1, direction="both")],
        ontology=load_ontology(STARTER),
    )


# --- resolution and configuration ---------------------------------------------


@pytest.mark.parametrize(
    ("requested", "resolved"),
    [("auto", "lexical"), ("lexical", "lexical"), ("substring", "substring")],
)
def test_resolve_search_mode(requested: str, resolved: str) -> None:
    assert resolve_search_mode(requested) == resolved


def test_resolve_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown search mode"):
        resolve_search_mode("vector")


def test_config_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_SEARCH_MODE", raising=False)
    assert RuntimeConfig.from_env().search_mode == "auto"


def test_config_accepts_case_insensitive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOS_SEARCH_MODE", " Substring ")
    assert RuntimeConfig.from_env().search_mode == "substring"


def test_config_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOS_SEARCH_MODE", "hybrid")
    with pytest.raises(ValueError, match="ONTOS_SEARCH_MODE"):
        RuntimeConfig.from_env()


# --- executor -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_executor_keeps_substring_keyword_seeds() -> None:
    executor = DeterministicExecutor()
    assert executor.keyword_search_mode == "substring"
    result = await executor.execute(_keyword_plan(), await _store())
    assert result.hits == []


@pytest.mark.asyncio
async def test_runtime_executor_uses_lexical_keyword_seeds() -> None:
    executor = build_verified_executor(replace(RuntimeConfig.from_env(), search_mode="auto"))
    assert executor.keyword_search_mode == "lexical"
    result = await executor.execute(_keyword_plan(), await _store())
    assert [hit.fact.id for hit in result.hits] == [UUID(int=1)]


def test_runtime_executor_honours_substring_revert() -> None:
    executor = build_verified_executor(replace(RuntimeConfig.from_env(), search_mode="substring"))
    assert executor.keyword_search_mode == "substring"


# --- ask audit ------------------------------------------------------------------


class _StubLLM:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _KeywordPlanner:
    id = "keyword-stub"

    def __init__(self, llm: object, ontology: object) -> None:
        pass

    async def plan(self, question: str, ontology: object) -> Plan:
        return _keyword_plan()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "resolved", "hits"), [("auto", "lexical", 1), ("substring", "substring", 0)]
)
async def test_ask_records_the_keyword_search_mode(
    monkeypatch: pytest.MonkeyPatch, configured: str, resolved: str, hits: int
) -> None:
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _KeywordPlanner)
    audit = AuditEmitter(signing_key=b"ask-search-mode")
    cfg = replace(
        RuntimeConfig.from_env(),
        planner_backend="ollama",
        ontology_path=STARTER,
        search_mode=configured,
    )
    server = build_server(cfg, store=await _store(), audit=audit)

    async with Client(server) as client:
        result = await client.call_tool("ask", {"question": "?", "agent_identity": "a"})

    assert len(result.data.payload["hits"]) == hits
    arguments = json.loads(audit._chain[-1].article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert arguments["search_mode"] == resolved
