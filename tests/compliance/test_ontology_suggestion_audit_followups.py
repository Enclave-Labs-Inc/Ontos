"""Gate-blocking: M2b suggestion audit follow-ups from #90.

- A cancelled model call left the audit trail at `request_started`.
- `generator_version` was the model tag, which can be re-pulled with
  different weights; generation used Ollama's sampling defaults and could
  overflow its default context window silently.
- A refused backend's outcome record didn't name it, so a trust refusal
  looked like a model failure.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from ontos.llm.ollama import (
    SUGGESTION_OPTIONS,
    OllamaBackend,
    SuggestionModelUnavailableError,
    SuggestionPromptTooLargeError,
)
from ontos.ontology.suggestion_requests import prompt_schema_digest
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.ontology_suggestions import list_ontology_suggestions
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance.test_ontology_suggestions import (
    _audit,
    _m2b_candidate,
    _ontology,
    _request_decisions,
    _reviewer,
)

_DIGEST = "ab" * 32


def _suggestion_records(audit: Any) -> list[dict[str, Any]]:
    return [
        {
            "decision": json.loads(r.article12[ArticleTwelveField.POLICY_DECISIONS])[-1]["result"],
            "model_versions": json.loads(r.article12[ArticleTwelveField.MODEL_VERSIONS]),
        }
        for r in audit._chain
        if r.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions"
    ]


def _scripted_ollama(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chat: Any = None,
    tags: dict[str, Any] | None = None,
) -> list[httpx.Request]:
    """Route OllamaBackend's own HTTP calls to a scripted local server."""
    requests: list[httpx.Request] = []
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            listing = (
                tags
                if tags is not None
                else {"models": [{"name": "gemma3:12b", "digest": f"sha256:{_DIGEST}"}]}
            )
            return httpx.Response(200, json=listing)
        reply = chat(request) if chat is not None else {"proposals": []}
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"message": {"content": json.dumps(reply)}})

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests


async def _run(audit: Any, backend: object) -> Any:
    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("followup-source", "acl:finance")])
    return await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=backend,  # type: ignore[arg-type]
    )


async def test_cancelled_model_call_leaves_an_outcome_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled(self: OllamaBackend, system: str, user: str, schema: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(OllamaBackend, "structured_suggest", cancelled)
    _scripted_ollama(monkeypatch)
    audit = _audit()

    with pytest.raises(asyncio.CancelledError):
        await _run(audit, OllamaBackend("gemma3:12b"))

    assert _request_decisions(audit) == ["request_started", "cancelled"]
    assert audit.verify_chain()


async def test_outcome_pins_model_digest_and_prompt_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def chat(request: httpx.Request) -> Any:
        proposal = {"candidate_id": 0, "kind": "new_relation_type", "relation_type": "advises"}
        return {"proposals": [proposal]}

    requests = _scripted_ollama(monkeypatch, chat=chat)
    audit = _audit()

    suggestions, _ = await _run(audit, OllamaBackend("gemma3:12b"))

    pinned = f"gemma3:12b@sha256:{_DIGEST}"
    outcome = _suggestion_records(audit)[-1]
    assert outcome["decision"] == "allow"
    assert outcome["model_versions"]["generator_version"] == pinned
    assert outcome["model_versions"]["prompt_schema_sha256"] == prompt_schema_digest(
        SUGGESTION_OPTIONS
    )
    assert [s.generator_version for s in suggestions if s.kind == "new_relation_type"] == [pinned]
    # Digest lookup happens once, before the model ever sees evidence.
    assert [r.url.path for r in requests] == ["/api/tags", "/api/chat"]


async def test_generation_is_greedy_seeded_and_sized(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _scripted_ollama(monkeypatch)

    await _run(_audit(), OllamaBackend("gemma3:12b"))

    chat = next(r for r in requests if r.url.path == "/api/chat")
    assert json.loads(chat.content)["options"] == {"temperature": 0, "seed": 0, "num_ctx": 16384}


async def test_oversized_prompt_is_refused_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _scripted_ollama(monkeypatch)
    backend = OllamaBackend("gemma3:12b")

    with pytest.raises(SuggestionPromptTooLargeError):
        await backend.structured_suggest("s", "x" * 40_000, {"type": "object"})

    assert requests == []


async def test_truncated_prompt_fails_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def chat(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "prompt_eval_count": SUGGESTION_OPTIONS["num_ctx"],
                "message": {"content": json.dumps({"proposals": []})},
            },
        )

    _scripted_ollama(monkeypatch, chat=chat)
    audit = _audit()

    with pytest.raises(SuggestionPromptTooLargeError):
        await _run(audit, OllamaBackend("gemma3:12b"))

    assert _request_decisions(audit) == ["request_started", "generation_error"]


async def test_model_not_installed_fails_before_reading_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted_ollama(monkeypatch, tags={"models": [{"name": "other:7b", "digest": "x"}]})
    audit = _audit()

    with pytest.raises(SuggestionModelUnavailableError):
        await _run(audit, OllamaBackend("gemma3:12b"))

    tools = [r.article12[ArticleTwelveField.TOOL_INVOKED] for r in audit._chain]
    assert "candidates" not in tools
    assert _request_decisions(audit) == ["request_started", "generation_error"]


@pytest.mark.parametrize(
    ("model", "listed"),
    [("gemma3", "gemma3:latest"), ("gemma3:12b", "gemma3:12b")],
)
async def test_digest_lookup_matches_tags(
    monkeypatch: pytest.MonkeyPatch, model: str, listed: str
) -> None:
    _scripted_ollama(monkeypatch, tags={"models": [{"name": listed, "digest": _DIGEST}]})

    assert await OllamaBackend(model).resolve_suggestion_version() == f"{model}@sha256:{_DIGEST}"


async def test_refused_backend_is_named_in_the_outcome() -> None:
    class Bridge:
        id = "openai:gpt-4o"
        version = "gpt-4o"
        sovereign = False

        async def structured_suggest(self, **_: Any) -> Any:
            raise AssertionError("a refused backend must never receive evidence")

    audit = _audit()

    with pytest.raises(Exception, match="sovereign"):
        await _run(audit, Bridge())

    outcome = _suggestion_records(audit)[-1]
    assert outcome["decision"] == "generation_error"
    assert outcome["model_versions"]["generator_id"] == "openai:gpt-4o"
    assert outcome["model_versions"]["generator_trust"] == "refused"
    assert "generator_version" not in outcome["model_versions"]
