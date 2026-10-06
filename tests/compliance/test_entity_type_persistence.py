"""#38 compliance: every (:Entity) node in the store carries a type.

Three invariants (per the issue body's acceptance criteria):

1. Round-trip — after `IngestPipeline.run()`, every entity node has the
   type the extractor assigned.
2. Migration safety — a pre-#38 store with type-less nodes (seeded via
   raw `add_fact`) picks up types on the next re-ingest without
   duplicating facts.
3. Resolver type-merge — when two Entity candidates with different
   types collide on the same id, last-write-wins and a structlog
   warning fires so operators can audit the overwrite via the
   resolver's `MergeRecord`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import structlog

from ontos.extraction.base import ExtractionResult
from ontos.ingest import SourceDocument
from ontos.ontology import load_ontology
from ontos.pipeline import ErrorPolicy, IngestPipeline
from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _prov(source_id: str = "doc1") -> Provenance:
    return Provenance(
        source_id=source_id,
        extractor_id="test-extractor",
        extractor_version="0.0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


def _fact(subject: str, predicate: str, obj: str, *, source_id: str = "doc1") -> Fact:
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=_prov(source_id),
        t_valid=datetime(2026, 1, 1),
        ingested_at=datetime(2026, 1, 1),
    )


class _FixtureConnector:
    """Yields a fixed sequence of SourceDocuments."""

    id = "fixture-conn"
    source_kind = "test"

    def __init__(self, docs: list[tuple[str, str]]) -> None:
        self._docs = docs

    async def iter_documents(self):
        for sid, text in self._docs:
            yield SourceDocument(source_id=sid, text=text)


class _FixtureExtractor:
    """Scripted extractor that yields fixed (entities, facts) per source_id."""

    id = "fixture-extractor"
    version = "0.0"

    def __init__(self, script: dict[str, tuple[list[Entity], list[Fact]]]) -> None:
        self._script = script

    async def extract(self, inp) -> ExtractionResult:
        entities, facts = self._script[inp.source_id]
        return ExtractionResult(entities=entities, facts=facts)


async def test_every_fact_entity_has_a_type_after_ingest() -> None:
    """Round-trip: facts' subject/object ids map to typed nodes."""
    ontology = load_ontology(STARTER)
    prov = _prov()
    entities = [
        Entity(id="alice", type="Person", canonical_name="Alice", provenance=prov),
        Entity(id="acme", type="Company", canonical_name="Acme Corp", provenance=prov),
    ]
    facts = [_fact("alice", "works_at", "acme")]

    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=_FixtureConnector([("doc1", "ignored")]),
        extractor=_FixtureExtractor({"doc1": (entities, facts)}),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    report = await pipeline.run()
    assert report.facts_written == 1

    # Every subject/object of every fact has a non-empty type.
    for fact_id, fact in store._facts.items():  # noqa: SLF001
        subj_attrs = store._graph.nodes[fact.subject_id]  # noqa: SLF001
        obj_attrs = store._graph.nodes[fact.object_id]  # noqa: SLF001
        assert subj_attrs.get("type"), f"fact {fact_id} subject {fact.subject_id} missing type"
        assert obj_attrs.get("type"), f"fact {fact_id} object {fact.object_id} missing type"

    assert store._graph.nodes["alice"]["type"] == "Person"  # noqa: SLF001
    assert store._graph.nodes["acme"]["type"] == "Company"  # noqa: SLF001


async def test_migration_safety_pre_38_nodes_gain_type_on_reingest() -> None:
    """A store seeded with pre-#38 type-less nodes (added via raw
    add_fact, bypassing upsert_entity) picks up types on the next
    ingest. Existing facts stay in place: no duplicate facts, no
    fact mutations."""
    ontology = load_ontology(STARTER)

    # Simulate a pre-#38 state: facts in the store, no entity types.
    store = NetworkxStore()
    old_fact = _fact("alice", "works_at", "acme")
    await store.add_fact(old_fact)

    assert "type" not in store._graph.nodes["alice"]  # noqa: SLF001
    assert "type" not in store._graph.nodes["acme"]  # noqa: SLF001
    pre_fact_count = len(store._facts)  # noqa: SLF001
    pre_fact_ids = set(store._facts)  # noqa: SLF001

    # Now a #38-aware ingest for the same entities. Must gain type;
    # must not duplicate the already-stored fact.
    prov = _prov()
    entities = [
        Entity(id="alice", type="Person", canonical_name="Alice", provenance=prov),
        Entity(id="acme", type="Company", canonical_name="Acme Corp", provenance=prov),
    ]
    # Note: ingesting a NEW fact separately — pre-#38 fact stays untouched.
    pipeline = IngestPipeline(
        connector=_FixtureConnector([("doc2", "ignored")]),
        extractor=_FixtureExtractor({"doc2": (entities, [])}),  # entities only
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    await pipeline.run()

    # Types applied.
    assert store._graph.nodes["alice"]["type"] == "Person"  # noqa: SLF001
    assert store._graph.nodes["acme"]["type"] == "Company"  # noqa: SLF001
    # Pre-existing fact untouched: same count, same id.
    assert len(store._facts) == pre_fact_count  # noqa: SLF001
    assert set(store._facts) == pre_fact_ids  # noqa: SLF001


async def test_resolver_type_merge_last_write_wins_with_audit_warning() -> None:
    """When two Entity candidates with different types resolve to the
    same id, the final node has a deterministic type (last-write-wins)
    AND a structlog warning records the overwrite so operators can
    audit via the resolver's MergeRecord."""
    ontology = load_ontology(STARTER)
    prov = _prov()
    # Two candidates, same id, different types.
    entities = [
        Entity(id="alice", type="Person", canonical_name="Alice", provenance=prov),
        Entity(id="alice", type="Company", canonical_name="Alice Inc", provenance=prov),
    ]

    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=_FixtureConnector([("doc1", "ignored")]),
        extractor=_FixtureExtractor({"doc1": (entities, [])}),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    with structlog.testing.capture_logs() as logs:
        await pipeline.run()

    # Last-write-wins — the second upsert's attrs persist.
    attrs = store._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["type"] == "Company"
    assert attrs["canonical_name"] == "Alice Inc"

    # Audit trail: the overwrite surfaced as a structlog warning.
    overwrites = [log for log in logs if log.get("event") == "entity-type-overwrite"]
    assert len(overwrites) == 1
    assert overwrites[0]["entity_id"] == "alice"
    assert overwrites[0]["previous_type"] == "Person"
    assert overwrites[0]["new_type"] == "Company"
