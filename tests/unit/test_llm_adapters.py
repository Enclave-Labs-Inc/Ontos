"""M5.b — Ollama + OpenAI + Anthropic adapter tests.

Sovereignty rule: CI never makes real outbound LLM calls. Both
adapters are exercised against mocked HTTP / SDK clients. Real
integration is a manual smoke-test the operator runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from ontos.extraction import LLMBackend, RawTriple
from ontos.llm import (
    AnthropicBackend,
    AnthropicRefusalError,
    OllamaBackend,
    OllamaTimeoutError,
    OpenAIBackend,
)
from ontos.ontology import Ontology, load_ontology
from ontos.planner import LLMPlannerBackend, SeedByEntity, SeedByKeyword

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


# -----------------------------------------------------------------------------
# Ollama
# -----------------------------------------------------------------------------


class _MockOllamaTransport(httpx.AsyncBaseTransport):
    """Returns a canned Ollama /api/chat response."""

    def __init__(self, response_json: str) -> None:
        self._response_json = response_json

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = {"message": {"content": self._response_json}}
        return httpx.Response(200, json=body)


def _ollama_client(response_json: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_MockOllamaTransport(response_json))


class _TimeoutTransport(httpx.AsyncBaseTransport):
    """Raises httpx.ReadTimeout on every request — simulates a slow Ollama."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout", request=request)


def _timeout_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_TimeoutTransport())


def test_ollama_conforms_to_both_protocols() -> None:
    backend = OllamaBackend("llama3.1:8b")
    assert isinstance(backend, LLMBackend)
    assert isinstance(backend, LLMPlannerBackend)


def test_ollama_id_and_version_include_model() -> None:
    backend = OllamaBackend("llama3.1:8b")
    assert backend.id == "ollama:llama3.1:8b"
    assert backend.version == "llama3.1:8b"


async def test_ollama_structured_extract_returns_raw_triples(
    ontology: Ontology,
) -> None:
    canned = (
        '{"triples": [{'
        '"subject_id": "person:alice",'
        '"subject_type": "Person",'
        '"subject_canonical_name": "Alice",'
        '"predicate": "works_at",'
        '"object_id": "company:acme",'
        '"object_type": "Company",'
        '"object_canonical_name": "Acme",'
        '"llm_confidence": 0.95'
        "}]}"
    )
    backend = OllamaBackend("llama3.1:8b", client=_ollama_client(canned))
    triples = await backend.structured_extract("Alice at Acme.", ontology)
    assert len(triples) == 1
    assert triples[0].subject_id == "person:alice"
    assert triples[0].llm_confidence == 0.95


async def test_ollama_empty_triples_returns_empty(ontology: Ontology) -> None:
    backend = OllamaBackend("llama3.1:8b", client=_ollama_client('{"triples": []}'))
    assert await backend.structured_extract("anything", ontology) == []


async def test_ollama_structured_plan_by_entity(ontology: Ontology) -> None:
    canned = (
        '{"seed": {"kind": "by_entity", "entity_id": "person:alice"}, '
        '"steps": [{"relations": ["works_at"], "depth": 1, "direction": "out"}], '
        '"limit": 5}'
    )
    backend = OllamaBackend("llama3.1:8b", client=_ollama_client(canned))
    plan = await backend.structured_plan("Where does Alice work?", ontology)
    assert isinstance(plan.seed, SeedByEntity)
    assert plan.seed.entity_id == "person:alice"
    assert plan.limit == 5
    assert plan.steps[0].relations == ["works_at"]


async def test_ollama_structured_plan_by_keyword(ontology: Ontology) -> None:
    canned = '{"seed": {"kind": "by_keyword", "keyword": "acme", "k": 3}, "steps": []}'
    backend = OllamaBackend("llama3.1:8b", client=_ollama_client(canned))
    plan = await backend.structured_plan("acme?", ontology)
    assert isinstance(plan.seed, SeedByKeyword)
    assert plan.seed.keyword == "acme"
    assert plan.seed.k == 3


async def test_ollama_unknown_seed_kind_raises(ontology: Ontology) -> None:
    canned = '{"seed": {"kind": "by_magic"}, "steps": []}'
    backend = OllamaBackend("llama3.1:8b", client=_ollama_client(canned))
    with pytest.raises(ValueError, match="unknown seed kind"):
        await backend.structured_plan("q", ontology)


async def test_ollama_structured_extract_wraps_read_timeout(ontology: Ontology) -> None:
    """#30: httpx.ReadTimeout → OllamaTimeoutError, with message naming the knob."""
    backend = OllamaBackend("llama3.1:8b", client=_timeout_client(), timeout_s=60.0)
    with pytest.raises(OllamaTimeoutError) as exc:
        await backend.structured_extract("some text", ontology)
    msg = str(exc.value)
    assert "60.0s" in msg
    assert "--ollama-timeout" in msg
    assert "api/chat" in msg
    assert exc.value.timeout_s == 60.0


async def test_ollama_structured_plan_wraps_read_timeout(ontology: Ontology) -> None:
    """Same wrap for the planner-side code path."""
    backend = OllamaBackend("llama3.1:8b", client=_timeout_client(), timeout_s=15.5)
    with pytest.raises(OllamaTimeoutError) as exc:
        await backend.structured_plan("q", ontology)
    assert "15.5s" in str(exc.value)


def test_ollama_timeout_s_defaults_to_sixty_seconds() -> None:
    """Protect against accidental default drift."""
    backend = OllamaBackend("llama3.1:8b")
    assert backend._timeout_s == 60.0  # noqa: SLF001 — explicit regression guard


# -----------------------------------------------------------------------------
# OpenAI
# -----------------------------------------------------------------------------


class _MockParsed:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed


class _MockChoice:
    def __init__(self, parsed: Any) -> None:
        self.message = _MockParsed(parsed)


class _MockCompletion:
    def __init__(self, parsed: Any) -> None:
        self.choices = [_MockChoice(parsed)]


class _MockChatCompletions:
    def __init__(self, canned_parsed_by_model: dict[str, Any]) -> None:
        self._canned = canned_parsed_by_model

    async def parse(self, *, model: str, messages: Any, response_format: Any) -> Any:
        return _MockCompletion(self._canned[response_format.__name__])


class _MockOpenAIClient:
    def __init__(self, canned: dict[str, Any]) -> None:
        self.chat = type("Chat", (), {"completions": _MockChatCompletions(canned)})()


def test_openai_conforms_to_both_protocols() -> None:
    backend = OpenAIBackend(client=object())  # any truthy client
    assert isinstance(backend, LLMBackend)
    assert isinstance(backend, LLMPlannerBackend)


def test_openai_id_and_version_include_model() -> None:
    backend = OpenAIBackend(model="gpt-4o-mini", client=object())
    assert backend.id == "openai:gpt-4o-mini"
    assert backend.version == "gpt-4o-mini"


async def test_openai_structured_extract_returns_triples(ontology: Ontology) -> None:
    from ontos.llm.openai import _ExtractionResponse

    parsed = _ExtractionResponse(
        triples=[
            RawTriple(
                subject_id="person:alice",
                subject_type="Person",
                subject_canonical_name="Alice",
                predicate="works_at",
                object_id="company:acme",
                object_type="Company",
                object_canonical_name="Acme",
                llm_confidence=0.9,
            )
        ]
    )
    client = _MockOpenAIClient({"_ExtractionResponse": parsed})
    backend = OpenAIBackend(client=client)
    triples = await backend.structured_extract("x", ontology)
    assert len(triples) == 1
    assert triples[0].llm_confidence == 0.9


async def test_openai_empty_parsed_returns_empty(ontology: Ontology) -> None:
    client = _MockOpenAIClient({"_ExtractionResponse": None})
    backend = OpenAIBackend(client=client)
    assert await backend.structured_extract("x", ontology) == []


async def test_openai_structured_plan_by_entity(ontology: Ontology) -> None:
    from ontos.llm.openai import _PlanResponse, _PlanSeed, _PlanStep

    parsed = _PlanResponse(
        seed=_PlanSeed(kind="by_entity", entity_id="person:alice"),
        steps=[_PlanStep(relations=["works_at"], depth=1, direction="out")],
        limit=5,
    )
    client = _MockOpenAIClient({"_PlanResponse": parsed})
    backend = OpenAIBackend(client=client)
    plan = await backend.structured_plan("q", ontology)
    assert isinstance(plan.seed, SeedByEntity)
    assert plan.seed.entity_id == "person:alice"


async def test_openai_none_parsed_plan_raises(ontology: Ontology) -> None:
    client = _MockOpenAIClient({"_PlanResponse": None})
    backend = OpenAIBackend(client=client)
    with pytest.raises(RuntimeError, match="no parsed plan"):
        await backend.structured_plan("q", ontology)


async def test_openai_by_entity_missing_entity_id_raises(ontology: Ontology) -> None:
    from ontos.llm.openai import _PlanResponse, _PlanSeed

    parsed = _PlanResponse(seed=_PlanSeed(kind="by_entity"), steps=[])
    client = _MockOpenAIClient({"_PlanResponse": parsed})
    backend = OpenAIBackend(client=client)
    with pytest.raises(ValueError, match="entity_id"):
        await backend.structured_plan("q", ontology)


async def test_openai_by_keyword_missing_keyword_raises(ontology: Ontology) -> None:
    from ontos.llm.openai import _PlanResponse, _PlanSeed

    parsed = _PlanResponse(seed=_PlanSeed(kind="by_keyword"), steps=[])
    client = _MockOpenAIClient({"_PlanResponse": parsed})
    backend = OpenAIBackend(client=client)
    with pytest.raises(ValueError, match="keyword"):
        await backend.structured_plan("q", ontology)


# -----------------------------------------------------------------------------
# Anthropic
# -----------------------------------------------------------------------------


class _MockUsage:
    def __init__(self, input_tokens: int = 100, output_tokens: int = 20) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 7
        self.cache_creation_input_tokens = None


class _MockStopDetails:
    def __init__(self, category: str | None) -> None:
        self.category = category


class _MockAnthropicResponse:
    def __init__(
        self,
        parsed: Any,
        *,
        stop_reason: str = "end_turn",
        category: str | None = None,
    ) -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = _MockStopDetails(category) if stop_reason == "refusal" else None
        self.model = "claude-opus-5"
        self.usage = _MockUsage()


class _MockAnthropicMessages:
    def __init__(self, responses_by_model: dict[str, _MockAnthropicResponse]) -> None:
        self._responses = responses_by_model
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> _MockAnthropicResponse:
        self.calls.append(kwargs)
        return self._responses[kwargs["output_format"].__name__]


class _MockAnthropicClient:
    def __init__(self, responses: dict[str, _MockAnthropicResponse]) -> None:
        self.messages = _MockAnthropicMessages(responses)
        self.beta = type("Beta", (), {"messages": self.messages})()


def _alice_works_at_acme() -> Any:
    from ontos.llm._structured import _ExtractionResponse

    return _ExtractionResponse(
        triples=[
            RawTriple(
                subject_id="person:alice",
                subject_type="Person",
                subject_canonical_name="Alice",
                predicate="works_at",
                object_id="company:acme",
                object_type="Company",
                object_canonical_name="Acme",
                llm_confidence=0.9,
            )
        ]
    )


def test_anthropic_conforms_to_both_protocols() -> None:
    backend = AnthropicBackend(client=object())
    assert isinstance(backend, LLMBackend)
    assert isinstance(backend, LLMPlannerBackend)


def test_anthropic_id_and_version_include_model() -> None:
    backend = AnthropicBackend(client=object())
    assert backend.id == "anthropic:claude-opus-5"
    assert backend.version == "claude-opus-5"


async def test_anthropic_structured_extract_returns_triples(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(_alice_works_at_acme())}
    )
    backend = AnthropicBackend(client=client)
    triples = await backend.structured_extract("Alice works at Acme.", ontology)
    assert len(triples) == 1
    assert triples[0].object_id == "company:acme"

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["messages"][0]["role"] == "user"
    assert "Alice works at Acme." in call["messages"][0]["content"]
    assert call["system"]


async def test_anthropic_enables_server_side_fallbacks_by_default(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(_alice_works_at_acme())}
    )
    await AnthropicBackend(client=client).structured_extract("x", ontology)
    call = client.messages.calls[0]
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


async def test_anthropic_fallbacks_can_be_disabled(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(_alice_works_at_acme())}
    )
    await AnthropicBackend(client=client, fallbacks=False).structured_extract("x", ontology)
    call = client.messages.calls[0]
    assert "fallbacks" not in call
    assert "betas" not in call


async def test_anthropic_structured_plan_by_keyword(ontology: Ontology) -> None:
    from ontos.llm._structured import _PlanResponse, _PlanSeed, _PlanStep

    parsed = _PlanResponse(
        seed=_PlanSeed(kind="by_keyword", keyword="acme", k=3),
        steps=[_PlanStep(relations=["works_at"], depth=1, direction="in")],
        limit=5,
    )
    client = _MockAnthropicClient({"_PlanResponse": _MockAnthropicResponse(parsed)})
    plan = await AnthropicBackend(client=client).structured_plan("who works at acme?", ontology)
    assert isinstance(plan.seed, SeedByKeyword)
    assert plan.seed.keyword == "acme"
    assert plan.limit == 5


async def test_anthropic_refusal_raises_typed_error(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {
            "_ExtractionResponse": _MockAnthropicResponse(
                None, stop_reason="refusal", category="cyber"
            )
        }
    )
    with pytest.raises(AnthropicRefusalError, match="cyber") as info:
        await AnthropicBackend(client=client).structured_extract("x", ontology)
    assert info.value.category == "cyber"


async def test_anthropic_max_tokens_raises(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(None, stop_reason="max_tokens")}
    )
    with pytest.raises(RuntimeError, match="max_tokens=256"):
        await AnthropicBackend(client=client, max_tokens=256).structured_extract("x", ontology)


async def test_anthropic_none_parsed_plan_raises(ontology: Ontology) -> None:
    client = _MockAnthropicClient({"_PlanResponse": _MockAnthropicResponse(None)})
    with pytest.raises(RuntimeError, match="no parsed plan"):
        await AnthropicBackend(client=client).structured_plan("q", ontology)


async def test_anthropic_usage_accumulates_across_calls(ontology: Ontology) -> None:
    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(_alice_works_at_acme())}
    )
    backend = AnthropicBackend(client=client)
    before = backend.usage.snapshot()
    await backend.structured_extract("x", ontology)
    await backend.structured_extract("y", ontology)
    delta = backend.usage - before
    assert delta.calls == 2
    assert delta.input_tokens == 200
    assert delta.output_tokens == 40
    assert delta.cache_read_input_tokens == 14
    assert delta.cache_creation_input_tokens == 0


async def test_anthropic_refusal_surfaces_as_extraction_error(ontology: Ontology) -> None:
    from ontos.extraction import ExtractionError, ExtractionInput, LlmExtractor

    client = _MockAnthropicClient(
        {"_ExtractionResponse": _MockAnthropicResponse(None, stop_reason="refusal")}
    )
    extractor = LlmExtractor(AnthropicBackend(client=client), ontology)
    with pytest.raises(ExtractionError, match="declined"):
        await extractor.extract(ExtractionInput(source_id="doc-1", text="x"))
