"""#90 correctness and UX: stable order, reported skips, CLI hints."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ontos.cli.main import _suggestion_failure_hint, _suggestion_generation_summary
from ontos.llm.ollama import (
    OllamaBackend,
    OllamaTimeoutError,
    SuggestionModelUnavailableError,
    SuggestionPromptTooLargeError,
)
from ontos.ontology.suggestion_backend import SuggestionBackendRefusedError
from ontos.runtime.ontology_suggestions import list_ontology_suggestions
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance.test_ontology_suggestions import (
    _audit,
    _candidate,
    _m2b_candidate,
    _ontology,
    _reviewer,
)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(self: OllamaBackend) -> str:
        return f"{self._model}@sha256:{'0' * 64}"

    monkeypatch.setattr(OllamaBackend, "resolve_suggestion_version", resolve)


def _with_predicate(source: str, acl: str, predicate: str) -> Any:
    candidate = _m2b_candidate(source, acl)
    return candidate.model_copy(
        update={"raw_triple": candidate.raw_triple.model_copy(update={"predicate": predicate})}
    )


async def _run(monkeypatch: pytest.MonkeyPatch, candidates: list[Any], proposals: Any) -> Any:
    async def scripted(self: OllamaBackend, system: str, user: str, schema: Any) -> Any:
        return proposals(user)

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)
    store = NetworkxStore()
    await store.add_candidates(candidates)
    return await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance", "acl:legal"),
        audit=_audit(),
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )


async def test_combined_output_order_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    def reversed_proposals(user: str) -> Any:
        evidence = json.loads(user)["evidence"]
        # The model answers in reverse order; output must not follow it.
        return {
            "proposals": [
                {
                    "candidate_id": e["candidate_id"],
                    "kind": "new_relation_type",
                    "relation_type": e["predicate"],
                }
                for e in reversed(evidence)
            ]
        }

    candidates = [
        _with_predicate("b-src", "acl:legal", "mentors"),
        _with_predicate("a-src", "acl:finance", "mentors"),
        _with_predicate("c-src", "acl:finance", "advises"),
        _candidate("d-src", "acl:finance"),
    ]
    suggestions, _ = await _run(monkeypatch, candidates, reversed_proposals)

    keys = [(s.acl_ref, s.kind, s.pattern.predicate if s.pattern else "") for s in suggestions]
    assert keys == sorted(keys)
    assert keys[0] == ("acl:finance", "add_pattern", "acquired")


async def test_non_snake_predicates_are_counted_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from ontos.runtime.models import ArticleTwelveField

    candidates = [
        _with_predicate("x-src", "acl:finance", "employedBy"),
        _with_predicate("y-src", "acl:finance", "advises"),
    ]
    suggestions, outcome = await _run(monkeypatch, candidates, lambda user: {"proposals": []})

    arguments = json.loads(outcome.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert arguments["skipped_count"] == 1
    summary = _suggestion_generation_summary(outcome)
    assert summary["mode"] == "llm-assisted"
    assert summary["skipped_count"] == 1
    assert summary["generator_version"] == f"scripted@sha256:{'0' * 64}"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("refused"), "`ollama serve`"),
        (SuggestionModelUnavailableError("x"), "`ollama pull gemma3:12b`"),
        (
            httpx.HTTPStatusError(
                "404",
                request=httpx.Request("POST", "http://127.0.0.1:11434/api/chat"),
                response=httpx.Response(404, text="SECRET response body"),
            ),
            "`ollama pull gemma3:12b`",
        ),
        (OllamaTimeoutError(30.0, "http://127.0.0.1:11434"), "--ollama-timeout"),
        (httpx.ReadTimeout("slow"), "--ollama-timeout"),
        (SuggestionPromptTooLargeError("x"), "context window"),
        (SuggestionBackendRefusedError("x"), "sovereignty policy"),
        (ValueError("SECRET internal detail"), "unexpected ValueError"),
    ],
)
def test_failure_hints_are_specific_and_body_free(exc: Exception, expected: str) -> None:
    hint = _suggestion_failure_hint(exc, "gemma3:12b")

    assert expected in hint
    assert "SECRET" not in hint
