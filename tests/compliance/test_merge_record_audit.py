"""#46 compliance: every MergeRecord the resolver cascade emits persists.

Three invariants:

1. End-to-end persistence — a full IngestPipeline run with a real
   `ExactMatchResolver` writes MergeRecords that survive the process.
2. Re-ingest idempotence — running the same ingest twice leaves one
   record (last-write-wins on resolved_at).
3. CascadeResolver pass-through — merges from sub-tiers flow unchanged
   through `cascade.py:47`'s concat.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ontos.extraction.base import ExtractionResult
from ontos.ingest import SourceDocument
from ontos.ontology import load_ontology
from ontos.pipeline import ErrorPolicy, IngestPipeline
from ontos.resolver import ExactMatchResolver
from ontos.resolver.cascade import CascadeResolver
from ontos.runtime.models import Confidence, Entity, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _prov() -> Provenance:
    return Provenance(
        source_id="doc1",
        extractor_id="test-extractor",
        extractor_version="0.0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


class _FixtureConnector:
    id = "fixture-conn"
    source_kind = "test"

    def __init__(self, docs: list[tuple[str, str]]) -> None:
        self._docs = docs

    async def iter_documents(self):
        for sid, text in self._docs:
            yield SourceDocument(source_id=sid, text=text)


class _FixtureExtractor:
    id = "fixture-extractor"
    version = "0.0"

    def __init__(self, script: dict[str, list[Entity]]) -> None:
        self._script = script

    async def extract(self, inp) -> ExtractionResult:
        return ExtractionResult(entities=self._script[inp.source_id], facts=[])


async def test_pipeline_persists_merge_record_through_exact_match_resolver(
    tmp_path: Path,
) -> None:
    """Full pipeline → ExactMatchResolver merges two candidates → the
    MergeRecord persists through the store and survives a close + re-open."""
    ontology = load_ontology(STARTER)
    prov = _prov()
    # Two candidates with same (type, canonical_name) → will merge.
    entities = [
        Entity(id="alice-1", type="Person", canonical_name="Alice", provenance=prov),
        Entity(id="alice-2", type="Person", canonical_name="Alice", provenance=prov),
    ]

    pkl = tmp_path / "store.pkl"
    store = NetworkxStore(path=pkl)
    pipeline = IngestPipeline(
        connector=_FixtureConnector([("doc1", "ignored")]),
        extractor=_FixtureExtractor({"doc1": entities}),
        resolver=ExactMatchResolver(),
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    report = await pipeline.run()
    assert report.merges_recorded == 1
    assert report.entities_merged == 1  # 2 candidates → 1 collapsed
    await store.close()

    # Second process reads the file; the merge survives.
    reader = NetworkxStore(path=pkl)
    canonical = entities[0].id  # ExactMatchResolver picks first in bucket
    non_canonical = entities[1].id
    hits = await reader.merges_for_entity(canonical)
    assert len(hits) == 1
    assert hits[0].resolver_id == "ontos.resolver.exact-match"
    # Normalized at write time — canonical stripped from merged_ids.
    assert hits[0].canonical_id == canonical
    assert hits[0].merged_ids == [non_canonical]

    hits_from_other = await reader.merges_for_entity(non_canonical)
    assert len(hits_from_other) == 1
    assert hits_from_other[0].canonical_id == canonical
    assert hits_from_other[0].merged_ids == [non_canonical]


async def test_re_ingest_is_idempotent_on_merge_persistence(tmp_path: Path) -> None:
    """Running the same ingest twice must leave one MergeRecord, with
    resolved_at reflecting the second run (last-write-wins)."""
    ontology = load_ontology(STARTER)
    prov = _prov()
    entities = [
        Entity(id="alice-1", type="Person", canonical_name="Alice", provenance=prov),
        Entity(id="alice-2", type="Person", canonical_name="Alice", provenance=prov),
    ]

    pkl = tmp_path / "store.pkl"

    async def _run_once() -> datetime:
        store = NetworkxStore(path=pkl)
        pipeline = IngestPipeline(
            connector=_FixtureConnector([("doc1", "ignored")]),
            extractor=_FixtureExtractor({"doc1": entities}),
            resolver=ExactMatchResolver(),
            store=store,
            ontology=ontology,
            error_policy=ErrorPolicy.FAIL_FAST,
        )
        await pipeline.run()
        hits = await store.merges_for_entity("alice-1")
        assert len(hits) == 1
        ts = hits[0].resolved_at
        await store.close()
        return ts

    first_resolved_at = await _run_once()
    second_resolved_at = await _run_once()

    # Idempotent: one record, timestamp from the latest run.
    reader = NetworkxStore(path=pkl)
    hits = await reader.merges_for_entity("alice-1")
    assert len(hits) == 1
    assert second_resolved_at >= first_resolved_at
    assert hits[0].resolved_at == second_resolved_at


async def test_cascade_resolver_merges_persist_unchanged() -> None:
    """CascadeResolver concatenates sub-tier merges — the pipeline must
    see the same records a flat ExactMatchResolver would emit."""
    ontology = load_ontology(STARTER)
    prov = _prov()
    entities = [
        Entity(id="A", type="Person", canonical_name="Ada", provenance=prov),
        Entity(id="B", type="Person", canonical_name="Ada", provenance=prov),
    ]

    store = NetworkxStore()
    cascade = CascadeResolver([ExactMatchResolver()])
    pipeline = IngestPipeline(
        connector=_FixtureConnector([("doc1", "ignored")]),
        extractor=_FixtureExtractor({"doc1": entities}),
        resolver=cascade,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    report = await pipeline.run()
    assert report.merges_recorded == 1
    hits = await store.merges_for_entity("A")
    assert len(hits) == 1
    assert hits[0].resolver_id == "ontos.resolver.exact-match"
    # Original resolver id survives the cascade pass-through.
