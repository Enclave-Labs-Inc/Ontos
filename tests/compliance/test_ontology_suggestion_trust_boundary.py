"""Gate-blocking: one explicit trust boundary for M2b suggestions (#90).

- The gate read `OllamaBackend._base_url` / `_client` directly; adapters
  now attest their own transport through a public method, and approval is
  an explicit registry of exact classes.
- The sovereignty marker was checked up to four times per request; the
  gate now mints a sealed token that downstream layers accept instead.
- `ontology.suggestion_trust` -> `llm.ollama` -> `ontology.suggestion_endpoint`
  formed an import cycle.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from ontos.llm.ollama import OllamaBackend
from ontos.ontology import suggestion_backend, suggestion_trust
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.ontology.suggestion_backend import SuggestionBackendRefusedError
from ontos.ontology.suggestion_requests import request_acl_isolated_suggestions
from ontos.ontology.suggestion_trust import (
    ApprovedSuggestionBackend,
    require_approved_suggestion_backend,
)
from ontos.runtime.ontology_suggestions import list_ontology_suggestions
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance.test_ontology_suggestions import _audit, _m2b_candidate, _reviewer


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(self: OllamaBackend) -> str:
        return f"{self._model}@sha256:{'0' * 64}"

    monkeypatch.setattr(OllamaBackend, "resolve_suggestion_version", resolve)


@pytest.mark.parametrize(
    "first",
    ["ontos.ontology.suggestion_trust", "ontos.llm.ollama", "ontos.runtime.ontology_suggestions"],
)
def test_no_import_cycle_whatever_is_imported_first(first: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _ontology(relation: str = "works_at") -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.1",
        entity_types=[EntityType(label="Person"), EntityType(label="Company")],
        relation_types=[RelationType(label=relation), RelationType(label="acquired")],
        patterns=[
            Pattern(subject_type="Person", predicate=relation, object_type="Company"),
            Pattern(subject_type="Company", predicate="acquired", object_type="Company"),
        ],
    )


async def _run(monkeypatch: pytest.MonkeyPatch, ontology: Ontology, reply: Any) -> Any:
    async def scripted(self: OllamaBackend, system: str, user: str, schema: Any) -> Any:
        return reply

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)
    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("trust-source", "acl:finance")])
    return await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=_audit(),
        ontology=ontology,
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )


async def test_sovereignty_is_checked_once_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    original = suggestion_backend.require_sovereign_suggestion_backend

    def spy(backend: object) -> None:
        calls.append(backend)
        original(backend)

    monkeypatch.setattr(suggestion_trust, "require_sovereign_suggestion_backend", spy)

    await _run(monkeypatch, _ontology(), {"proposals": []})

    assert len(calls) == 1


def test_token_cannot_be_forged() -> None:
    class Spoof:
        id = "ollama:x"
        version = "x"
        sovereign = True

    with pytest.raises(SuggestionBackendRefusedError, match="trust gate"):
        ApprovedSuggestionBackend(
            backend=Spoof(),  # type: ignore[arg-type]
            endpoint="http://127.0.0.1:11434",
            _seal=object(),
        )


async def test_requests_refuse_a_raw_backend() -> None:
    with pytest.raises(SuggestionBackendRefusedError, match="trust gate"):
        await request_acl_isolated_suggestions(
            authorized_candidates=[],
            ontology=_ontology(),
            backend=OllamaBackend("scripted"),  # type: ignore[arg-type]
        )


def test_registered_class_must_still_attest_its_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Scribe:
        id = "scribe:local"
        version = "1"
        sovereign = True

        async def structured_suggest(self, **_: Any) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(
        suggestion_trust,
        "APPROVED_SUGGESTION_BACKENDS",
        (*suggestion_trust.APPROVED_SUGGESTION_BACKENDS, Scribe),
    )

    with pytest.raises(SuggestionBackendRefusedError, match="attest"):
        require_approved_suggestion_backend(Scribe())  # type: ignore[arg-type]

    Scribe.attest_local_transport = lambda self: "http://127.0.0.1:9000"  # type: ignore[attr-defined]
    approved = require_approved_suggestion_backend(Scribe())  # type: ignore[arg-type]
    assert approved.endpoint == "http://127.0.0.1:9000"


async def test_mapping_to_an_existing_uppercase_relation_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neo4j-style labels are valid mapping targets; membership makes them safe."""
    reply = {
        "proposals": [{"candidate_id": 0, "kind": "map_to_existing", "relation_type": "WORKS_AT"}]
    }

    suggestions, outcome = await _run(monkeypatch, _ontology("WORKS_AT"), reply)

    mapped = [s for s in suggestions if s.kind == "map_to_existing"]
    assert [s.mapped_relation_type for s in mapped] == ["WORKS_AT"]
    assert [s.observed_predicate for s in mapped] == ["advises"]
    from ontos.runtime.models import ArticleTwelveField

    assert json.loads(outcome.article12[ArticleTwelveField.TOOL_ARGUMENTS])["invalid_count"] == 0
