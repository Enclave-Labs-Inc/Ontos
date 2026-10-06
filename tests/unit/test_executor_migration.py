"""#34 — DeterministicExecutor migration-registry integration tests.

Verifies the read-side projection: facts flowing through
`_expand` are translated by the registry before PPR scoring,
drops are honored, and warnings propagate into
`ExecutionResult.warnings` for Article-12 audit.
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


@pytest.fixture
async def store_with_v01_fact() -> NetworkxStore:
    """Store populated with a single v0.1 fact (`works_at` under
    ontology v0.1), ready to be queried against a v0.2 target."""
    store = NetworkxStore()
    now = datetime.now(UTC)
    await store.add_fact(
        Fact(
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
    )
    return store


def _plan() -> Plan:
    return Plan(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(direction="out", depth=1)],
    )


async def test_executor_translates_facts_through_registry(
    store_with_v01_fact: NetworkxStore,
) -> None:
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
    result = await executor.execute(_plan(), store_with_v01_fact)

    assert len(result.hits) == 1
    assert result.hits[0].fact.predicate == "employed_by"
    # Bitemporal immutability: the stored fact still reads as works_at
    # when fetched without the registry.
    plain_executor = DeterministicExecutor()
    plain_result = await plain_executor.execute(_plan(), store_with_v01_fact)
    assert plain_result.hits[0].fact.predicate == "works_at"


async def test_executor_without_registry_preserves_pre_34_behavior(
    store_with_v01_fact: NetworkxStore,
) -> None:
    executor = DeterministicExecutor()
    result = await executor.execute(_plan(), store_with_v01_fact)
    assert len(result.hits) == 1
    assert result.hits[0].fact.predicate == "works_at"
    # No migration warnings when the registry isn't wired.
    assert not any("ontology-migration" in w for w in result.warnings)


async def test_executor_warnings_carry_migration_trail(
    store_with_v01_fact: NetworkxStore,
) -> None:
    registry = MigrationRegistry()
    # Register nothing — the 0.1 → 0.2 path is unreachable, so the
    # registry emits a `no-migration-path` warning per fact.
    executor = DeterministicExecutor(
        current_ontology=_ontology_v02(),
        migration_registry=registry,
    )
    result = await executor.execute(_plan(), store_with_v01_fact)

    assert len(result.hits) == 1  # non-strict pass-through
    assert any("no-migration-path" in w for w in result.warnings)
    # Warning includes the fact id so an auditor can trace which
    # specific fact lacked a migration path.
    fact_id = result.hits[0].fact.id
    assert any(str(fact_id) in w for w in result.warnings)


async def test_executor_honors_strict_mode_drops(
    store_with_v01_fact: NetworkxStore,
) -> None:
    registry = MigrationRegistry()
    registry.register(
        PredicateDeprecate(
            from_version="0.1",
            to_version="0.2",
            predicate="works_at",
        )
    )
    executor = DeterministicExecutor(
        current_ontology=_ontology_v02(),
        migration_registry=registry,
    )
    result = await executor.execute(_plan(), store_with_v01_fact)
    # The only fact in the store is deprecated → nothing to rank.
    # The seed expansion sees the fact, but the migration drops it
    # before PPR, so expansion produces no facts and the executor
    # emits "expansion produced no facts".
    assert result.hits == []
    assert any("migration-dropped-fact" in w for w in result.warnings)
