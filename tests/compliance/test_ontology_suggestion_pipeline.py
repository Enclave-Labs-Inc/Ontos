"""Scripted end-to-end M2b generation compliance tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ontos.extraction import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
)
from ontos.llm.ollama import OllamaBackend
from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    RelationType,
)
from ontos.ontology.suggestion_backend import (
    SuggestionBackendRefusedError,
)
from ontos.ontology.suggestion_endpoint import (
    UntrustedSuggestionEndpointError,
)
from ontos.ontology.suggestion_pipeline import generate_llm_suggestions
from ontos.storage.base import StoredCandidate

KEY = b"test-derived-reference-key"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _ontology() -> Ontology:
    return Ontology(
        id="test.ontology",
        version="1.0",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[RelationType(label="works_at")],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            )
        ],
    )


def _candidate(
    source: str,
    acl: str | None,
    predicate: str,
) -> StoredCandidate:
    return StoredCandidate(
        stored_id=f"PRIVATE-STORAGE-{source}",
        candidate=RejectedCandidate(
            raw_triple=CandidateTriple(
                subject_id=f"person-{source}",
                subject_type="Person",
                subject_canonical_name="Private Person",
                predicate=predicate,
                object_id=f"company-{source}",
                object_type="Company",
                object_canonical_name="Private Company",
                llm_confidence=0.9,
            ),
            reason="pattern_not_in_ontology",
            provenance=CandidateProvenance(
                source_id=source,
                extractor_id="extractor",
                extractor_version="v1",
                ontology_id="test.ontology",
                ontology_version="1.0",
                confidence_score=0.9,
            ),
            acl_ref=acl,
        ),
    )


@pytest.mark.asyncio
async def test_acl_isolation_and_server_generated_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        payload = json.loads(user)
        calls.append(payload)

        proposals = []
        for item in payload["evidence"]:
            predicate = item["predicate"]
            proposals.append(
                {
                    "candidate_id": item["candidate_id"],
                    "kind": ("map_to_existing" if predicate == "manages" else "new_relation_type"),
                    "relation_type": ("works_at" if predicate == "manages" else predicate),
                }
            )

        return {"proposals": proposals}

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    backend = OllamaBackend("scripted")
    result = await generate_llm_suggestions(
        authorized_candidates=[
            _candidate("f1", "finance", "manages"),
            _candidate("l1", "legal", "owns"),
            _candidate("p1", None, "reviews"),
        ],
        ontology=_ontology(),
        backend=backend,
        reference_key=KEY,
        generated_at=NOW,
    )

    assert len(calls) == 3
    assert {frozenset(item["predicate"] for item in call["evidence"]) for call in calls} == {
        frozenset({"manages"}),
        frozenset({"owns"}),
        frozenset({"reviews"}),
    }

    assert result.invalid_count == 0
    assert len(result.suggestions) == 3
    assert {s.acl_ref for s in result.suggestions} == {"finance", "legal", None}

    for suggestion in result.suggestions:
        assert suggestion.support_count == 1
        assert len(suggestion.supporting_candidate_refs) == 1
        assert suggestion.generator_id == "ollama:scripted"
        assert suggestion.generator_version == "scripted"
        assert suggestion.ontology_id == "test.ontology"
        assert suggestion.ontology_version == "1.0"
        assert suggestion.generated_at == NOW
        assert all("PRIVATE-STORAGE" not in ref for ref in suggestion.supporting_candidate_refs)


@pytest.mark.asyncio
async def test_injected_model_metadata_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def injected(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                    "acl_ref": "admin",
                    "support_count": 999,
                    "ontology_version": "999",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", injected)

    result = await generate_llm_suggestions(
        authorized_candidates=[_candidate("f1", "finance", "manages")],
        ontology=_ontology(),
        backend=OllamaBackend("scripted"),
        reference_key=KEY,
    )

    assert result.suggestions == ()
    assert result.invalid_count == 1


@pytest.mark.asyncio
async def test_untrusted_endpoint_refused_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        pytest.fail("Untrusted endpoint received model evidence")

    monkeypatch.setattr(OllamaBackend, "structured_suggest", forbidden)

    with pytest.raises(UntrustedSuggestionEndpointError):
        await generate_llm_suggestions(
            authorized_candidates=[_candidate("f1", "finance", "manages")],
            ontology=_ontology(),
            backend=OllamaBackend(
                "scripted",
                base_url="https://external.example:11434",
            ),
            reference_key=KEY,
        )


@pytest.mark.asyncio
async def test_bridge_refused_even_with_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")

    class Bridge:
        id = "bridge"
        version = "1"
        sovereign = False

        async def structured_suggest(
            self, *, system: str, user: str, schema: dict[str, Any]
        ) -> dict[str, Any]:
            pytest.fail("Bridge received candidate evidence")

    with pytest.raises(SuggestionBackendRefusedError):
        await generate_llm_suggestions(
            authorized_candidates=[_candidate("f1", "finance", "manages")],
            ontology=_ontology(),
            backend=Bridge(),
            reference_key=KEY,
        )


@pytest.mark.asyncio
async def test_later_acl_failure_aborts_entire_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1

        if calls == 2:
            raise RuntimeError("Scripted backend failure")

        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    with pytest.raises(RuntimeError, match="Scripted backend failure"):
        await generate_llm_suggestions(
            authorized_candidates=[
                _candidate("f1", "finance", "manages"),
                _candidate("l1", "legal", "owns"),
            ],
            ontology=_ontology(),
            backend=OllamaBackend("scripted"),
            reference_key=KEY,
        )

    assert calls == 2
