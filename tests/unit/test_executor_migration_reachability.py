"""Strict migrations must not allow traversal through rejected edges."""

from datetime import UTC, datetime

import pytest

from ontos.executor import DeterministicExecutor
from ontos.migration import MigrationRegistry
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _ontology() -> Ontology:
    return Ontology(
        id="ontos.test",
        version="0.2",
        entity_types=[EntityType(label="Person")],
        relation_types=[RelationType(label="related_to")],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="related_to",
                object_type="Person",
            )
        ],
    )


def _fact(
    subject: str,
    object_id: str,
    version: str,
) -> Fact:
    now = datetime.now(UTC)

    return Fact(
        subject_id=subject,
        predicate="related_to",
        object_id=object_id,
        provenance=Provenance(
            source_id="test-document",
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.test",
            ontology_version=version,
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


@pytest.mark.parametrize("relation_filter", [False, True])
async def test_rejected_edge_cannot_connect_other_facts(
    relation_filter: bool,
) -> None:
    """Valid two-hop paths survive; paths via rejected edges do not."""
    store = NetworkxStore()

    # This edge has no migration path to the active v0.2 ontology.
    rejected = _fact("person:a", "person:b", "0.0")

    # Individually valid, but connected to A only via rejected edge.
    disconnected = _fact("person:b", "person:c", "0.2")

    # A separate, entirely valid two-hop path.
    direct = _fact("person:a", "person:d", "0.2")
    second_hop = _fact("person:d", "person:e", "0.2")

    try:
        for fact in (rejected, disconnected, direct, second_hop):
            await store.add_fact(fact)

        step = TraversalStep(
            direction="out",
            depth=2,
            relations=["related_to"] if relation_filter else [],
        )

        plan = Plan(
            seed=SeedByEntity(entity_id="person:a"),
            steps=[step],
        )

        executor = DeterministicExecutor(
            current_ontology=_ontology(),
            migration_registry=MigrationRegistry(strict=True),
        )

        warnings: list[str] = []

        collected, distances = await executor._expand(
            plan,
            ["person:a"],
            store,
            None,
            None,
            None,
            warnings,
        )

        assert set(collected) == {direct.id, second_hop.id}
        assert rejected.id not in collected
        assert disconnected.id not in collected

        assert distances[direct.id] == 1
        assert distances[second_hop.id] == 2

        assert any("no-migration-path" in warning for warning in warnings)

        # Migration filtering must never alter stored facts.
        for fact in (rejected, disconnected, direct, second_hop):
            assert await store.get_fact(fact.id) == fact
    finally:
        await store.close()
