"""M1.d — LlmExtractor tests using a deterministic FakeLLMBackend.

Real-LLM integration (OpenAI/Anthropic/Vertex adapters, or Enclave
Scribe) is exercised manually by the operator against their own
credentials, per the sovereignty invariant in CLAUDE.md — CI must not
depend on outbound calls to third-party providers.

These tests pin the wrapper contract:
- provenance is populated on every emitted fact
- schema-strict filtering drops out-of-ontology triples with warnings
- silent-empty extraction is a loud error, not an empty result
- confidence calibration maps raw scores to the right label
- entities are deduped across triples in a single call
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.extraction import (
    ExtractionError,
    ExtractionInput,
    Extractor,
    LlmExtractor,
    RawTriple,
)
from ontos.ontology import Ontology, load_ontology
from ontos.runtime.models import Confidence

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


class FakeLLM:
    """LLMBackend that returns pre-programmed responses.

    Fixed order: each call to `structured_extract` pops the next
    programmed response and returns it. Raising anything programs the
    LLM to raise on that call — useful for testing error paths.
    """

    id: str = "fake-llm"
    version: str = "test-1"

    def __init__(self, responses: list[list[RawTriple] | Exception]) -> None:
        self._responses = list(responses)

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        if not self._responses:
            raise AssertionError("FakeLLM ran out of programmed responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


def _valid_triple(**overrides: object) -> RawTriple:
    defaults = {
        "subject_id": "person:alice",
        "subject_type": "Person",
        "subject_canonical_name": "Alice Smith",
        "predicate": "works_at",
        "object_id": "company:acme",
        "object_type": "Company",
        "object_canonical_name": "Acme Corp",
        "llm_confidence": 0.95,
    }
    defaults.update(overrides)
    return RawTriple(**defaults)  # type: ignore[arg-type]


async def test_extractor_conforms_to_protocol(ontology: Ontology) -> None:
    extractor = LlmExtractor(FakeLLM([]), ontology)
    assert isinstance(extractor, Extractor)


async def test_extractor_id_includes_llm_id(ontology: Ontology) -> None:
    extractor = LlmExtractor(FakeLLM([]), ontology)
    assert "fake-llm" in extractor.id
    assert extractor.version == "test-1"


async def test_happy_path_produces_facts_with_provenance(ontology: Ontology) -> None:
    llm = FakeLLM([[_valid_triple()]])
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(
        ExtractionInput(source_id="doc-1", text="Alice works at Acme.")
    )

    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.subject_id == "person:alice"
    assert fact.predicate == "works_at"
    assert fact.object_id == "company:acme"
    # Provenance populated end-to-end
    assert fact.provenance.source_id == "doc-1"
    assert "fake-llm" in fact.provenance.extractor_id
    assert fact.provenance.confidence == Confidence.EXTRACTED
    assert fact.provenance.confidence_score == 0.95
    # #34: ontology stamp lands from the active ontology on BOTH the
    # Fact's provenance AND every extracted Entity's provenance.
    assert fact.provenance.ontology_id == ontology.id
    assert fact.provenance.ontology_version == ontology.version
    assert result.entities
    for ent in result.entities:
        assert ent.provenance.ontology_id == ontology.id
        assert ent.provenance.ontology_version == ontology.version


async def test_silent_empty_on_non_empty_input_raises(ontology: Ontology) -> None:
    llm = FakeLLM([[]])
    extractor = LlmExtractor(llm, ontology)
    with pytest.raises(ExtractionError) as exc:
        await extractor.extract(ExtractionInput(source_id="doc-2", text="Some real text."))
    assert exc.value.source_id == "doc-2"
    assert "zero triples" in str(exc.value)


async def test_empty_input_returns_empty_result_without_error(ontology: Ontology) -> None:
    llm = FakeLLM([[]])
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="doc-3", text="   "))
    assert result.facts == []
    assert result.entities == []


async def test_schema_violation_drops_triple_with_warning(ontology: Ontology) -> None:
    # `works_at` from Company to Person is not a declared pattern —
    # only Person -works_at-> Company is.
    llm = FakeLLM(
        [
            [
                _valid_triple(
                    subject_id="company:acme",
                    subject_type="Company",
                    subject_canonical_name="Acme Corp",
                    object_id="person:alice",
                    object_type="Person",
                    object_canonical_name="Alice Smith",
                )
            ]
        ]
    )
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="doc-4", text="Acme employs Alice."))
    assert result.facts == []
    assert len(result.warnings) == 1
    assert "not in the ontology" in result.warnings[0]


async def test_out_of_ontology_predicate_drops_triple(ontology: Ontology) -> None:
    llm = FakeLLM(
        [
            [
                _valid_triple(
                    predicate="haunts",  # not declared
                    object_type="Person",
                    object_id="person:bob",
                    object_canonical_name="Bob",
                )
            ]
        ]
    )
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="doc-5", text="Alice haunts Bob."))
    assert result.facts == []
    assert len(result.rejected) == 1
    candidate = result.rejected[0]
    assert candidate.reason == "pattern_not_in_ontology"
    assert candidate.raw_triple.predicate == "haunts"
    assert candidate.provenance.source_id == "doc-5"
    assert candidate.provenance.ontology_id == ontology.id
    assert candidate.provenance.ontology_version == ontology.version
    assert "haunts" not in str(result.warnings)
    assert "person:alice" not in str(result.warnings)
    assert "person:bob" not in str(result.warnings)


async def test_below_min_confidence_dropped_with_warning(ontology: Ontology) -> None:
    llm = FakeLLM([[_valid_triple(llm_confidence=0.2)]])
    extractor = LlmExtractor(llm, ontology, min_confidence=0.5)
    result = await extractor.extract(ExtractionInput(source_id="doc-6", text="Maybe."))
    assert result.facts == []
    assert any("min_confidence" in w for w in result.warnings)


@pytest.mark.parametrize(
    "raw,expected_label",
    [
        (1.00, Confidence.EXTRACTED),
        (0.95, Confidence.EXTRACTED),
        (0.90, Confidence.EXTRACTED),
        (0.89, Confidence.INFERRED),
        (0.60, Confidence.INFERRED),
        (0.59, Confidence.AMBIGUOUS),
        (0.00, Confidence.AMBIGUOUS),
    ],
)
async def test_confidence_calibration(
    ontology: Ontology, raw: float, expected_label: Confidence
) -> None:
    llm = FakeLLM([[_valid_triple(llm_confidence=raw)]])
    # Use min_confidence=0 so the very low ones still get through.
    extractor = LlmExtractor(llm, ontology, min_confidence=0.0)
    result = await extractor.extract(ExtractionInput(source_id="d", text="t"))
    assert len(result.facts) == 1
    assert result.facts[0].provenance.confidence == expected_label
    assert result.facts[0].provenance.confidence_score == raw


async def test_entities_are_deduped_across_triples(ontology: Ontology) -> None:
    # Alice appears in two triples; result should have one Alice entity.
    llm = FakeLLM(
        [
            [
                _valid_triple(object_id="company:acme"),
                _valid_triple(object_id="company:foo", object_canonical_name="Foo Inc"),
            ]
        ]
    )
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="d", text="t"))
    entity_ids = [e.id for e in result.entities]
    assert entity_ids.count("person:alice") == 1
    assert "company:acme" in entity_ids
    assert "company:foo" in entity_ids


async def test_every_fact_references_an_entity_in_the_result(ontology: Ontology) -> None:
    llm = FakeLLM([[_valid_triple(), _valid_triple(object_id="company:foo")]])
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="d", text="t"))
    entity_ids = {e.id for e in result.entities}
    for fact in result.facts:
        assert fact.subject_id in entity_ids
        assert fact.object_id in entity_ids


async def test_upstream_exception_wraps_as_extraction_error(ontology: Ontology) -> None:
    """#30: raw backend exceptions get wrapped so IngestPipeline's error
    policy handles them cleanly. Pre-#30 they escaped as raw httpx /
    provider stack traces."""

    class BoomError(RuntimeError):
        pass

    llm = FakeLLM([BoomError("upstream API down")])
    extractor = LlmExtractor(llm, ontology)
    with pytest.raises(ExtractionError, match="LLM backend failed") as exc:
        await extractor.extract(ExtractionInput(source_id="d", text="t"))
    # Original exception remains accessible without appearing
    # in ordinary traceback formatting.
    assert exc.value.__cause__ is None
    assert isinstance(exc.value.__context__, BoomError)
    assert exc.value.__suppress_context__ is True


async def test_ollama_timeout_wraps_as_extraction_error_naming_knob(
    ontology: Ontology,
) -> None:
    """#30: OllamaTimeoutError gets a dedicated branch so its message
    (which already names --ollama-timeout) survives into the
    operator-visible ExtractionError."""
    from ontos.llm import OllamaTimeoutError

    llm = FakeLLM([OllamaTimeoutError(60.0, "http://localhost:11434/api/chat")])
    extractor = LlmExtractor(llm, ontology)
    with pytest.raises(ExtractionError, match="--ollama-timeout") as exc:
        await extractor.extract(ExtractionInput(source_id="doc1", text="t"))
    assert "60.0s" in str(exc.value)
    assert isinstance(exc.value.__cause__, OllamaTimeoutError)


async def test_extraction_error_from_backend_is_not_rewrapped(
    ontology: Ontology,
) -> None:
    """ExtractionError from a backend passes through unchanged — don't
    double-wrap."""
    original = ExtractionError(source_id="d", message="backend raised", sample="t")
    llm = FakeLLM([original])
    extractor = LlmExtractor(llm, ontology)
    with pytest.raises(ExtractionError) as exc:
        await extractor.extract(ExtractionInput(source_id="d", text="t"))
    assert exc.value is original


async def test_extraction_input_source_id_becomes_provenance_source_id(
    ontology: Ontology,
) -> None:
    llm = FakeLLM([[_valid_triple()]])
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(
        ExtractionInput(source_id="sec-edgar:0001234-25-000001", text="Alice at Acme.")
    )
    assert result.facts[0].provenance.source_id == "sec-edgar:0001234-25-000001"
    assert result.entities[0].provenance.source_id == "sec-edgar:0001234-25-000001"


async def test_bitemporal_defaults_set(ontology: Ontology) -> None:
    llm = FakeLLM([[_valid_triple()]])
    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(ExtractionInput(source_id="d", text="t"))
    fact = result.facts[0]
    # t_valid and ingested_at both set (extractor uses now() when the
    # source doesn't carry a stronger claim); t_invalid open.
    assert fact.t_valid is not None
    assert fact.ingested_at is not None
    assert fact.t_invalid is None


async def test_type_prefix_is_removed_from_canonical_name_but_raw_id_is_preserved(
    ontology: Ontology,
) -> None:
    llm = FakeLLM(
        [
            [
                _valid_triple(
                    subject_id="Person:Alice Johnson",
                    subject_canonical_name="Person:Alice Johnson",
                    object_id="Company:Acme Corp",
                    object_canonical_name="Company:Acme Corp",
                )
            ]
        ]
    )

    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(
        ExtractionInput(
            source_id="type-prefix-test",
            text="Alice Johnson works at Acme Corp.",
        )
    )

    entities = {entity.id: entity for entity in result.entities}

    assert entities["Person:Alice Johnson"].canonical_name == "Alice Johnson"
    assert entities["Company:Acme Corp"].canonical_name == "Acme Corp"

    # Current Ontos pre-resolution semantics intentionally preserve raw IDs.
    assert result.facts[0].subject_id == "Person:Alice Johnson"
    assert result.facts[0].object_id == "Company:Acme Corp"


async def test_type_suffix_is_removed_from_canonical_name_but_raw_id_is_preserved(
    ontology: Ontology,
) -> None:
    llm = FakeLLM(
        [
            [
                _valid_triple(
                    subject_id="Alice Johnson (Person)",
                    subject_canonical_name="Alice Johnson (Person)",
                    object_id="Acme Corp (Company)",
                    object_canonical_name="Acme Corp (Company)",
                )
            ]
        ]
    )

    extractor = LlmExtractor(llm, ontology)
    result = await extractor.extract(
        ExtractionInput(
            source_id="type-suffix-test",
            text="Alice Johnson works at Acme Corp.",
        )
    )

    entities = {entity.id: entity for entity in result.entities}

    assert entities["Alice Johnson (Person)"].canonical_name == "Alice Johnson"
    assert entities["Acme Corp (Company)"].canonical_name == "Acme Corp"

    # Raw IDs remain untouched under current pre-resolution semantics.
    assert result.facts[0].subject_id == "Alice Johnson (Person)"
    assert result.facts[0].object_id == "Acme Corp (Company)"


async def test_rejects_raw_id_reused_for_different_entities(
    ontology: Ontology,
) -> None:
    llm = FakeLLM(
        [
            [
                _valid_triple(
                    subject_id="same-id",
                    subject_type="Company",
                    subject_canonical_name="Acme Corp",
                    predicate="subsidiary_of",
                    object_id="same-id",
                    object_type="Company",
                    object_canonical_name="Beta Systems",
                )
            ]
        ]
    )

    extractor = LlmExtractor(llm, ontology)

    with pytest.raises(ExtractionError, match="raw entity id"):
        await extractor.extract(
            ExtractionInput(
                source_id="collision-test",
                text="Beta Systems is a subsidiary of Acme Corp.",
            )
        )


async def test_rejected_candidate_captures_low_confidence(
    ontology: Ontology,
) -> None:
    raw = _valid_triple(llm_confidence=0.2)
    extractor = LlmExtractor(FakeLLM([[raw]]), ontology, min_confidence=0.5)

    result = await extractor.extract(
        ExtractionInput(source_id="restricted-doc", text="Sensitive text")
    )

    assert result.facts == []
    assert result.entities == []
    assert len(result.rejected) == 1

    candidate = result.rejected[0]
    assert candidate.reason == "below_min_confidence"
    assert candidate.raw_triple.subject_id == raw.subject_id
    assert candidate.raw_triple.predicate == raw.predicate
    assert candidate.raw_triple.object_id == raw.object_id
    assert candidate.provenance.source_id == "restricted-doc"
    assert candidate.provenance.extractor_id == extractor.id
    assert candidate.provenance.extractor_version == extractor.version
    assert candidate.provenance.ontology_id == ontology.id
    assert candidate.provenance.ontology_version == ontology.version
    assert candidate.provenance.confidence_score == 0.2

    warnings = " ".join(result.warnings)
    assert "person:alice" not in warnings
    assert "company:acme" not in warnings
    assert "works_at" not in warnings
    assert "restricted-doc" not in warnings


async def test_mixed_valid_and_rejected_triples(
    ontology: Ontology,
) -> None:
    valid = _valid_triple()
    invalid = _valid_triple(predicate="secret_relationship")
    low_confidence = _valid_triple(
        object_id="company:other",
        object_canonical_name="Other Company",
        llm_confidence=0.1,
    )

    extractor = LlmExtractor(
        FakeLLM([[valid, invalid, low_confidence]]),
        ontology,
        min_confidence=0.5,
    )

    result = await extractor.extract(ExtractionInput(source_id="doc-mixed", text="Test"))

    assert len(result.facts) == 1
    assert result.facts[0].predicate == "works_at"
    assert len(result.entities) == 2
    assert len(result.rejected) == 2

    assert [c.reason for c in result.rejected] == [
        "pattern_not_in_ontology",
        "below_min_confidence",
    ]

    warnings = " ".join(result.warnings)
    assert "secret_relationship" not in warnings
    assert "company:other" not in warnings
    assert "person:alice" not in warnings


async def test_valid_extraction_has_no_rejected_candidates(
    ontology: Ontology,
) -> None:
    extractor = LlmExtractor(FakeLLM([[_valid_triple()]]), ontology)

    result = await extractor.extract(
        ExtractionInput(source_id="doc-valid", text="Alice works at Acme")
    )

    assert len(result.facts) == 1
    assert result.rejected == []
    assert result.warnings == []


async def test_generic_backend_exception_does_not_expose_sensitive_details(
    ontology: Ontology,
) -> None:
    secret = "SECRET_PRIVATE_DOCUMENT_CONTENT"
    backend_error = RuntimeError(f"provider returned: {secret}")
    extractor = LlmExtractor(FakeLLM([backend_error]), ontology)

    with pytest.raises(ExtractionError, match="LLM backend failed") as caught:
        await extractor.extract(ExtractionInput(source_id="doc-1", text="Private document."))

    import traceback

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    # What an operator sees on FAIL_FAST: the full printed traceback.
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is backend_error


async def test_backend_failure_logging_excludes_sensitive_content(
    ontology: Ontology,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    logger = Mock()
    monkeypatch.setattr(
        "ontos.extraction.llm_extractor.log",
        logger,
    )

    secret = "SECRET_BACKEND_RESPONSE"
    document = "PRIVATE_DOCUMENT_CONTENT"

    backend_error = RuntimeError(f"provider returned {secret}")
    extractor = LlmExtractor(FakeLLM([backend_error]), ontology)

    with pytest.raises(ExtractionError) as caught:
        await extractor.extract(
            ExtractionInput(
                source_id="document-123",
                text=document,
            )
        )

    assert caught.value.kind == "backend"
    assert caught.value.__context__ is backend_error
    assert caught.value.__suppress_context__ is True
    assert secret not in str(caught.value)

    logger.warning.assert_called_once_with(
        "llm-backend-failed",
        source_id="document-123",
        error_type="RuntimeError",
    )

    logged = repr(logger.warning.call_args)
    assert secret not in logged
    assert document not in logged
