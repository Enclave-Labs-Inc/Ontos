"""Gate-blocking: `ask` is configurable under `ontos serve` and never echoes
planner errors (#101).

Nothing built a planner for the server, so `ask` always answered "no
planner configured". On a planning failure it returned `str(exc)`,
model- or provider-derived text, to the caller and wrote it into the
audit record.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from fastmcp import Client

import ontos.llm
import ontos.planner
from ontos.audit.emitter import AuditEmitter
from ontos.planner import Plan, PlannerError, SeedByEntity
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.server import build_planner, build_server
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
SECRET = "SECRET-PROVIDER-BODY api_key=sk-live-123"


def _cfg(**overrides: object) -> RuntimeConfig:
    return replace(RuntimeConfig.from_env(), **overrides)  # type: ignore[arg-type]


class _StubLLM:
    def __init__(self, model: str, **kwargs: object) -> None:
        self.model = model
        self.kwargs = kwargs


class _StubPlanner:
    id = "stub-planner"

    def __init__(self, llm: _StubLLM, ontology: object) -> None:
        self.llm = llm

    async def plan(self, question: str, ontology: object) -> Plan:
        return Plan.build(seed=SeedByEntity(entity_id="x"), steps=[], ontology=ontology)  # type: ignore[arg-type]


class _FailingPlanner(_StubPlanner):
    async def plan(self, question: str, ontology: object) -> Plan:
        raise PlannerError(question, SECRET)


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _StubPlanner)


def test_planner_is_disabled_by_default() -> None:
    assert build_planner(_cfg(planner_backend="none")) is None


def test_planner_requires_an_ontology() -> None:
    with pytest.raises(RuntimeError, match="ONTOS_ONTOLOGY_PATH"):
        build_planner(_cfg(planner_backend="ollama", ontology_path=None))


def test_only_the_local_backend_is_offered() -> None:
    with pytest.raises(RuntimeError, match="not supported"):
        build_planner(_cfg(planner_backend="anthropic", ontology_path=STARTER))


def test_planner_env_reaches_the_backend(stubbed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOS_PLANNER_BACKEND", "Ollama")
    monkeypatch.setenv("ONTOS_PLANNER_MODEL", "gemma3:12b")
    monkeypatch.setenv("ONTOS_PLANNER_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("ONTOS_PLANNER_TIMEOUT", "180")
    monkeypatch.setenv("ONTOS_ONTOLOGY_PATH", str(STARTER))

    planner = build_planner(RuntimeConfig.from_env())

    assert isinstance(planner, _StubPlanner)
    assert planner.llm.model == "gemma3:12b"
    assert planner.llm.kwargs == {"base_url": "http://127.0.0.1:11434", "timeout_s": 180.0}


async def test_ask_answers_when_the_planner_is_configured(stubbed: None) -> None:
    audit = AuditEmitter(signing_key=b"ask-planner")
    cfg = _cfg(planner_backend="ollama", ontology_path=STARTER)
    server = build_server(cfg, store=NetworkxStore(), audit=audit)

    async with Client(server) as client:
        result = await client.call_tool("ask", {"question": "who?", "agent_identity": "a"})

    payload = result.data.payload
    assert "error" not in payload
    assert payload["plan"]["seed"]["entity_id"] == "x"


async def test_planner_errors_never_reach_caller_audit_or_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _FailingPlanner)
    audit = AuditEmitter(signing_key=b"ask-planner")
    cfg = _cfg(planner_backend="ollama", ontology_path=STARTER)
    server = build_server(cfg, store=NetworkxStore(), audit=audit)

    with structlog.testing.capture_logs() as logs:
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "who?", "agent_identity": "a"})

    assert result.data.payload == {"error": "the question could not be planned"}
    record = audit._chain[-1].article12
    assert SECRET not in json.dumps({k.value: v for k, v in record.items()})
    decisions = json.loads(record[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions[-1] == {"policy": "planner", "result": "error", "error_type": "PlannerError"}
    assert SECRET not in json.dumps(logs, default=str)
