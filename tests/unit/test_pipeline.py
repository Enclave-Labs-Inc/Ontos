"""M5.a — IngestPipeline tests using in-process fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.extraction import ExtractionError, LlmExtractor, RawTriple
from ontos.ingest import TextConnector
from ontos.ontology import Ontology, load_ontology
from ontos.pipeline import ErrorPolicy, IngestPipeline
from ontos.resolver import ExactMatchResolver
from ontos.storage.networkx_store import NetworkxStore

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


class ScriptedLLM:
    id: str = "pipeline-fake"
    version: str = "0"

    def __init__(self, script: dict[str, list[RawTriple]]) -> None:
        self._script = script

    async def structured_extract(
        self, text: str, ontology: Ontology
    ) -> list[RawTriple]:
        return list(self._script.get(text, []))


def _triple(
    subject: str, s_type: str, s_name: str,
    predicate: str,
    obj: str, o_type: str, o_name: str,
) -> RawTriple:
    return RawTriple(
        subject_id=subject,
        subject_type=s_type,
        subject_canonical_name=s_name,
        predicate=predicate,
        object_id=obj,
        object_type=o_type,
        object_canonical_name=o_name,
        llm_confidence=0.95,
    )


async def test_run_writes_facts_to_store(ontology: Ontology) -> None:
    corpus = {
        "doc-alice": "Alice at Acme.",
        "doc-acquire": "Acme acquired Widget.",
    }
    script = {
        "Alice at Acme.": [
            _triple(
                "person:alice", "Person", "Alice",
                "works_at",
                "company:acme", "Company", "Acme",
            )
        ],
        "Acme acquired Widget.": [
            _triple(
                "company:acme", "Company", "Acme",
                "acquired",
                "company:widget", "Company", "Widget",
            )
        ],
    }
    connector = TextConnector(corpus)
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=ExactMatchResolver(),
        store=store,
    )
    report = await pipeline.run()

    assert report.documents_seen == 2
    assert report.documents_extracted == 2
    assert report.facts_written == 2
    assert report.errors == []
    # And the facts are actually queryable
    hits = await store.search("acquired")
    assert len(hits) == 1


async def test_acl_ref_from_source_propagates_to_facts(ontology: Ontology) -> None:
    corpus = {"doc-hr": "Alice at Acme."}
    script = {
        "Alice at Acme.": [
            _triple(
                "person:alice", "Person", "Alice",
                "works_at",
                "company:acme", "Company", "Acme",
            )
        ]
    }
    connector = TextConnector(corpus, acl_ref_by_source={"doc-hr": "acl:hr"})
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=None,
        store=store,
    )
    await pipeline.run()

    # Only alice with allowed_acls=['acl:hr'] can see it; deny-by-default otherwise.
    private = await store.search("works_at", allowed_acls=["acl:hr"])
    public_attempt = await store.search("works_at", allowed_acls=[])
    assert len(private) == 1
    assert public_attempt == []


async def test_fail_fast_on_extractor_error(ontology: Ontology) -> None:
    corpus = {"doc-broken": "not scripted"}
    script: dict[str, list[RawTriple]] = {}  # ScriptedLLM returns empty -> ExtractionError
    connector = TextConnector(corpus)
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=None,
        store=store,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    with pytest.raises(ExtractionError):
        await pipeline.run()


async def test_skip_and_log_continues_past_errors(ontology: Ontology) -> None:
    corpus = {
        "doc-good": "Alice at Acme.",
        "doc-broken": "unscripted, will error",
    }
    script = {
        "Alice at Acme.": [
            _triple(
                "person:alice", "Person", "Alice",
                "works_at",
                "company:acme", "Company", "Acme",
            )
        ],
    }
    connector = TextConnector(corpus)
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=None,
        store=store,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    )
    report = await pipeline.run()

    assert report.documents_seen == 2
    assert report.documents_extracted == 1  # broken one skipped
    assert report.facts_written == 1
    assert len(report.errors) == 1
    assert report.errors[0].source_id == "doc-broken"
    assert "zero triples" in report.errors[0].reason.lower()


async def test_report_captures_extractor_warnings(ontology: Ontology) -> None:
    corpus = {"doc-1": "text"}
    script = {
        "text": [
            # A triple with an out-of-ontology pattern → dropped + warning.
            # LlmExtractor doesn't raise here (raw_triples was non-empty);
            # it just drops the invalid one and emits a warning.
            _triple(
                "company:a", "Company", "A",
                "works_at",  # works_at from Company to Person is not declared
                "person:b", "Person", "B",
            ),
        ]
    }
    connector = TextConnector(corpus)
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=None,
        store=store,
    )
    report = await pipeline.run()

    assert report.documents_seen == 1
    assert report.documents_extracted == 1  # extraction succeeded (just produced 0 valid facts)
    assert report.facts_written == 0
    assert report.errors == []  # not an ExtractionError — schema drop is a warning
    assert any("not in the ontology" in w for w in report.warnings)


async def test_no_resolver_skips_merges_but_still_writes_facts(
    ontology: Ontology,
) -> None:
    corpus = {"doc-1": "Alice at Acme."}
    script = {
        "Alice at Acme.": [
            _triple(
                "person:alice", "Person", "Alice",
                "works_at",
                "company:acme", "Company", "Acme",
            )
        ]
    }
    connector = TextConnector(corpus)
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=None,  # explicit
        store=store,
    )
    report = await pipeline.run()
    assert report.entities_merged == 0
    assert report.facts_written == 1


async def test_empty_corpus_returns_empty_report(ontology: Ontology) -> None:
    connector = TextConnector({})
    extractor = LlmExtractor(ScriptedLLM({}), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=ExactMatchResolver(),
        store=store,
    )
    report = await pipeline.run()
    assert report.documents_seen == 0
    assert report.documents_extracted == 0
    assert report.facts_written == 0
    assert report.errors == []


async def test_pipeline_report_is_frozen(ontology: Ontology) -> None:
    import pydantic

    connector = TextConnector({})
    extractor = LlmExtractor(ScriptedLLM({}), ontology)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=connector, extractor=extractor, resolver=None, store=store,
    )
    report = await pipeline.run()
    with pytest.raises(pydantic.ValidationError):
        report.facts_written = 99  # type: ignore[misc]
