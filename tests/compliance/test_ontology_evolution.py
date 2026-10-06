"""#34 — End-to-end ontology-evolution compliance.

Shields the two load-bearing invariants:

1. **Bitemporal immutability** — a fact written under ontology v0.1
   stays `works_at` in storage forever; migration is a read-side
   projection, never a mutation.
2. **Article-12 audit** — every migration event lands in
   `ExecutionResult.warnings` so a regulator tracing an answer can
   reconstruct which facts were translated or dropped, and by which
   migration.

Also pins the full pipeline wiring: ingest under v0.1 →
`MigrationRegistry` registered against v0.2 → query returns the
v0.2 projection without rewriting the store.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontos.executor import DeterministicExecutor
from ontos.migration import MigrationRegistry, PredicateDeprecate, PredicateRename
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _ontology_v02() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.2",
        entity_types=[EntityType(label="Person"), EntityType(label="Company")],
        relation_types=[RelationType(label="employed_by")],
        patterns=[Pattern(subject_type="Person", predicate="employed_by", object_type="Company")],
    )


async def _store_with_v01_fact() -> tuple[NetworkxStore, Fact]:
    store = NetworkxStore()
    now = datetime.now(UTC)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0.0.0",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )
    await store.add_fact(fact)
    return store, fact


@pytest.mark.asyncio
async def test_ingest_under_v01_query_under_v02_returns_translated_predicate() -> None:
    store, stored_fact = await _store_with_v01_fact()
    registry = MigrationRegistry()
    registry.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    executor = DeterministicExecutor(
        current_ontology=_ontology_v02(),
        migration_registry=registry,
    )
    result = await executor.execute(
        Plan(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[TraversalStep(direction="out", depth=1)],
        ),
        store,
    )
    assert len(result.hits) == 1
    assert result.hits[0].fact.predicate == "employed_by"
    # And a direct store read is untouched: the stored fact's predicate
    # is still "works_at" (bitemporal immutability).
    reread = await store.get_fact(stored_fact.id)
    assert reread is not None
    assert reread.predicate == "works_at"


@pytest.mark.asyncio
async def test_stored_fact_predicate_is_immutable_through_migration() -> None:
    store, stored_fact = await _store_with_v01_fact()
    registry = MigrationRegistry()
    registry.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    executor = DeterministicExecutor(
        current_ontology=_ontology_v02(),
        migration_registry=registry,
    )
    # Run the migration ten times; the stored fact never drifts.
    plan = Plan(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(direction="out", depth=1)],
    )
    for _ in range(10):
        await executor.execute(plan, store)
    reread = await store.get_fact(stored_fact.id)
    assert reread is not None
    assert reread.predicate == "works_at"
    # Provenance ontology stamp also unchanged — the fact's lineage
    # keeps naming the schema it was extracted under, which is the
    # whole point of #34.
    assert reread.provenance.ontology_version == "0.1"


@pytest.mark.asyncio
async def test_migration_warnings_enable_article12_audit_trail() -> None:
    store, stored_fact = await _store_with_v01_fact()
    # Mixed registry: works_at renamed, acquired deprecated. Build a
    # second fact so we see one translation + one drop in the trail.
    now = datetime.now(UTC)
    extra = Fact(
        subject_id="person:alice",
        predicate="acquired",
        object_id="company:widget",
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0.0.0",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )
    await store.add_fact(extra)

    registry = MigrationRegistry()
    registry.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    registry.register(
        PredicateDeprecate(
            from_version="0.1",
            to_version="0.2",
            predicate="acquired",
        )
    )
    executor = DeterministicExecutor(
        current_ontology=_ontology_v02(),
        migration_registry=registry,
    )
    result = await executor.execute(
        Plan(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[TraversalStep(direction="out", depth=1)],
        ),
        store,
    )
    # Translation side: works_at survives as employed_by.
    predicates = {hit.fact.predicate for hit in result.hits}
    assert predicates == {"employed_by"}
    # Drop side: the deprecate migration emitted a warning naming the
    # dropped fact id so an auditor can trace the migration event.
    assert any("migration-dropped-fact" in w and str(extra.id) in w for w in result.warnings)
