"""M1.a — Extractor Protocol + ExtractionInput/Result models.

These tests pin the Scribe-drop-in contract. If they fail after a change
to `ontos.extraction.base`, the change has broken the boundary — either
fix the change or update this test with a matching CLAUDE.md note.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ontos.extraction import (
    ExtractionError,
    ExtractionInput,
    ExtractionResult,
    Extractor,
)
from ontos.runtime.models import Confidence, Entity, Fact, Provenance


def _provenance(source_id: str = "test-source", score: float = 0.9) -> Provenance:
    return Provenance(
        source_id=source_id,
        extractor_id="test-extractor",
        extractor_version="0.0.0",
        confidence=Confidence.EXTRACTED,
        confidence_score=score,
    )


def _entity(entity_id: str, entity_type: str = "Thing") -> Entity:
    return Entity(
        id=entity_id,
        type=entity_type,
        canonical_name=entity_id,
        provenance=_provenance(),
    )


def _fact(subject_id: str, predicate: str, object_id: str) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        provenance=_provenance(),
        t_valid=now,
        ingested_at=now,
    )


class _StubExtractor:
    """Reference implementation used to prove the Protocol type-checks.

    Any real extractor (LlamaIndex, LangChain, Scribe) must expose this
    same surface. If a new attribute is added to `Extractor`, this stub
    breaks first and forces the interface change to be conscious.
    """

    id: str = "stub"
    version: str = "0.0.0"

    async def extract(self, input: ExtractionInput) -> ExtractionResult:
        entity = _entity(f"ent:{input.source_id}")
        return ExtractionResult(entities=[entity], facts=[], warnings=[])


def test_stub_conforms_to_extractor_protocol() -> None:
    stub = _StubExtractor()
    # runtime_checkable Protocol — this actually verifies the shape.
    assert isinstance(stub, Extractor)


async def test_stub_extract_returns_valid_result() -> None:
    stub = _StubExtractor()
    result = await stub.extract(ExtractionInput(source_id="doc-1", text="hello world"))
    assert len(result.entities) == 1
    assert result.entities[0].id == "ent:doc-1"


def test_extraction_input_is_frozen() -> None:
    import pydantic

    inp = ExtractionInput(source_id="s1", text="t")
    try:
        inp.text = "changed"  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("ExtractionInput must be frozen")


def test_extraction_result_is_frozen() -> None:
    import pydantic

    res = ExtractionResult()
    try:
        res.warnings = ["nope"]  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("ExtractionResult must be frozen")


def test_extraction_result_defaults_are_empty_lists() -> None:
    res = ExtractionResult()
    assert res.entities == []
    assert res.facts == []
    assert res.warnings == []


def test_extraction_input_metadata_defaults_empty() -> None:
    inp = ExtractionInput(source_id="s", text="t")
    assert inp.metadata == {}


def test_entity_is_frozen() -> None:
    import pydantic

    ent = _entity("ent:1")
    try:
        ent.canonical_name = "changed"  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("Entity must be frozen")


def test_extraction_error_carries_source_id_and_truncates_sample() -> None:
    long_sample = "x" * 2000
    err = ExtractionError(source_id="doc-42", message="LLM returned empty", sample=long_sample)
    assert err.source_id == "doc-42"
    assert len(err.sample) == 500
    assert "doc-42" in str(err)
    assert "LLM returned empty" in str(err)


def test_extraction_error_is_a_runtime_error() -> None:
    # Callers should be able to catch it as RuntimeError if they want to
    # log-and-continue on a per-source basis. It must NOT be caught by a
    # bare Exception handler in the request path — see CLAUDE.md.
    err = ExtractionError(source_id="s", message="m")
    assert isinstance(err, RuntimeError)


def test_result_fact_subject_and_object_reference_entities_in_same_result() -> None:
    # Structural invariant — enforced by the extractor contract, checked
    # here at the model level: an ExtractionResult where a Fact points
    # at an entity id that isn't in the entities list is malformed.
    # (We don't enforce this in Pydantic itself because the check needs
    # cross-field access; enforcement lives in the extractor impls.)
    ent_a = _entity("ent:a")
    ent_b = _entity("ent:b")
    fact = _fact("ent:a", "knows", "ent:b")
    result = ExtractionResult(entities=[ent_a, ent_b], facts=[fact])
    entity_ids = {e.id for e in result.entities}
    for f in result.facts:
        assert f.subject_id in entity_ids
        assert f.object_id in entity_ids


class _BadStubMissingId:
    version: str = "0.0.0"

    async def extract(self, input: ExtractionInput) -> ExtractionResult:
        return ExtractionResult()


def test_incomplete_stub_does_not_satisfy_protocol() -> None:
    bad = _BadStubMissingId()
    assert not isinstance(bad, Extractor)


def test_fact_defaults_id_when_extractor_forgets() -> None:
    # UUID auto-assigned; extractors don't need to supply one.
    fact = _fact("a", "knows", "b")
    assert fact.id  # non-empty UUID


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_provenance_accepts_valid_confidence_scores(score: float) -> None:
    prov = _provenance(score=score)
    assert prov.confidence_score == score


@pytest.mark.parametrize("bad_score", [-0.01, 1.01, 2.0])
def test_provenance_rejects_out_of_range_scores(bad_score: float) -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _provenance(score=bad_score)


def test_provenance_is_required_on_entity() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Entity(id="e", type="T", canonical_name="e")  # type: ignore[call-arg]


def test_stub_extractor_id_and_version_are_readable() -> None:
    # Small but load-bearing: `id` and `version` appear on every Fact
    # this extractor produces. Missing either would silently break audit.
    stub = _StubExtractor()
    assert stub.id == "stub"
    assert stub.version == "0.0.0"


def test_uuid_import_available_in_this_test_context() -> None:
    # Belt-and-braces — used elsewhere, keeping the module boundary loud.
    assert uuid4()
