"""M3.b — DeterministicExecutor tests against the NetworkX store.

We use NetworkxStore (M0/M1) as the substrate for unit tests — it
satisfies the GraphStore Protocol identically to Neo4jStore, so the
executor code exercised here is the same code that runs in
production. Neo4j is covered separately in tests/integration/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.executor import DeterministicExecutor, ExecutionResult, Executor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import (
    Plan,
    SeedByEntity,
    SeedByKeyword,
    TraversalStep,
)
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


@pytest.fixture
async def populated_store() -> NetworkxStore:
    store = NetworkxStore()
    now = datetime.now(UTC)

    def _mk(subject: str, predicate: str, obj: str, score: float = 1.0) -> Fact:
        return Fact(
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            provenance=Provenance(
                source_id="t",
                extractor_id="t",
                extractor_version="0.0.0",
                confidence=Confidence.EXTRACTED,
                confidence_score=score,
            ),
            t_valid=now,
            ingested_at=now,
        )

    # A chain: alice --works_at-> acme --acquired-> widget --subsidiary_of-> foo
    await store.add_fact(_mk("person:alice", "works_at", "company:acme"))
    await store.add_fact(_mk("company:acme", "acquired", "company:widget"))
    await store.add_fact(_mk("company:widget", "subsidiary_of", "company:foo"))
    # A distractor
    await store.add_fact(_mk("person:bob", "works_at", "company:distractor"))
    return store


def test_executor_conforms_to_protocol() -> None:
    assert isinstance(DeterministicExecutor(), Executor)


async def test_execute_from_entity_seed_returns_ranked_hits(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=3)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    assert isinstance(result, ExecutionResult)
    assert len(result.hits) >= 1
    # Hits should be ranked descending by combined_score.
    scores = [h.combined_score for h in result.hits]
    assert scores == sorted(scores, reverse=True)


async def test_execute_from_keyword_seed_uses_search_to_find_entities(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByKeyword(keyword="acme", k=3),
        steps=[TraversalStep(depth=2)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    entities_in_results = {h.fact.subject_id for h in result.hits} | {
        h.fact.object_id for h in result.hits
    }
    assert "company:acme" in entities_in_results


async def test_execute_returns_empty_when_seed_finds_nothing(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:ghost"),
        steps=[TraversalStep(depth=2)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    assert result.hits == []
    assert result.warnings  # expansion produced no facts


async def test_execute_respects_plan_limit(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=5)],
        limit=2,
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    assert len(result.hits) <= 2


async def test_execute_filters_by_relation_when_step_specifies(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(relations=["works_at"], depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    predicates = {h.fact.predicate for h in result.hits}
    assert predicates.issubset({"works_at"})


async def test_ppr_boosts_facts_near_the_seed(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=5)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    # The nearest fact (alice --works_at-> acme) should out-rank the farthest
    # (widget --subsidiary_of-> foo).
    by_predicate = {h.fact.predicate: h for h in result.hits}
    assert "works_at" in by_predicate
    if "subsidiary_of" in by_predicate:
        assert (
            by_predicate["works_at"].combined_score >= by_predicate["subsidiary_of"].combined_score
        )


async def test_path_score_decays_by_hops(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=5)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    # Path score for a 1-hop fact should be >= 3-hop path score, all else equal.
    hops_to_path_score: dict[int, float] = {}
    for h in result.hits:
        hops_to_path_score.setdefault(h.hops_from_seed, h.path_score)
    if 1 in hops_to_path_score and 3 in hops_to_path_score:
        assert hops_to_path_score[1] >= hops_to_path_score[3]


async def test_early_stop_drops_low_flow_facts(ontology: Ontology) -> None:
    store = NetworkxStore()
    now = datetime.now(UTC)

    def _mk_low(subject: str, predicate: str, obj: str) -> Fact:
        return Fact(
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            provenance=Provenance(
                source_id="t",
                extractor_id="t",
                extractor_version="0.0.0",
                confidence=Confidence.AMBIGUOUS,
                confidence_score=0.01,  # very low confidence
            ),
            t_valid=now,
            ingested_at=now,
        )

    await store.add_fact(_mk_low("person:alice", "works_at", "company:x"))
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, store)
    # With confidence 0.01, path_score < 0.05 → dropped by early-stop.
    assert result.hits == []


async def test_result_and_hit_are_frozen(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    import pydantic

    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    with pytest.raises(pydantic.ValidationError):
        result.hits = []  # type: ignore[misc]
    if result.hits:
        with pytest.raises(pydantic.ValidationError):
            result.hits[0].combined_score = 0.0  # type: ignore[misc]


async def test_executor_honors_default_bidirectional_traversal(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="company:acme"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )

    assert plan.steps[0].direction == "both"

    result = await DeterministicExecutor().execute(plan, populated_store)

    predicates = {hit.fact.predicate for hit in result.hits}
    assert "works_at" in predicates
    assert "acquired" in predicates


async def test_executor_honors_incoming_traversal_direction(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="company:acme"),
        steps=[
            TraversalStep(
                relations=["works_at"],
                depth=1,
                direction="in",
            )
        ],
        ontology=ontology,
    )

    result = await DeterministicExecutor().execute(plan, populated_store)

    assert any(
        hit.fact.subject_id == "person:alice"
        and hit.fact.predicate == "works_at"
        and hit.fact.object_id == "company:acme"
        for hit in result.hits
    )


async def test_multi_step_respects_directional_frontier(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    now = datetime.now(UTC)
    await populated_store.add_fact(
        Fact(
            subject_id="person:david",
            predicate="works_at",
            object_id="company:widget",
            provenance=Provenance(
                source_id="t",
                extractor_id="t",
                extractor_version="0.0.0",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=now,
            ingested_at=now,
        )
    )

    plan = Plan.build(
        seed=SeedByEntity(entity_id="company:acme"),
        steps=[
            TraversalStep(
                relations=["acquired"],
                depth=1,
                direction="out",
            ),
            TraversalStep(
                relations=["works_at"],
                depth=1,
                direction="in",
            ),
        ],
        ontology=ontology,
    )

    result = await DeterministicExecutor().execute(plan, populated_store)

    works_at_subjects = {
        hit.fact.subject_id for hit in result.hits if hit.fact.predicate == "works_at"
    }

    assert "person:david" in works_at_subjects
    assert "person:alice" not in works_at_subjects


async def test_multi_step_expansion_accumulates_hops(
    populated_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[
            TraversalStep(relations=["works_at"], depth=1),
            TraversalStep(relations=["acquired"], depth=1),
        ],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, populated_store)
    predicates = {h.fact.predicate for h in result.hits}
    assert predicates <= {"works_at", "acquired"}


async def test_executor_id_is_stable_and_versioned() -> None:
    e1 = DeterministicExecutor()
    e2 = DeterministicExecutor()
    assert e1.id == e2.id
    assert "v" in e1.id  # some version marker
