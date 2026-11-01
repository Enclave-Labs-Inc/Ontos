"""#103: a multi-depth step reports each fact's real distance from the seed.

`_expand` stamped every fact a step returned with one hop, so with
`TraversalStep(depth=3)` a fact three hops out scored like a direct
neighbour and `hops_from_seed` was wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


def _fact(subject: str, obj: str) -> Fact:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Fact(
        subject_id=subject,
        predicate="acquired",
        object_id=obj,
        provenance=Provenance(
            source_id="t",
            extractor_id="t",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def _chain() -> tuple[NetworkxStore, list[Fact]]:
    store = NetworkxStore()
    chain = [_fact("a", "b"), _fact("b", "c"), _fact("c", "d")]
    for fact in chain:
        await store.add_fact(fact)
    return store, chain


@pytest.mark.parametrize("direction", ["out", "both"])
async def test_one_deep_step_reports_true_hops(ontology: Ontology, direction: str) -> None:
    store, (ab, bc, cd) = await _chain()
    plan = Plan.build(
        seed=SeedByEntity(entity_id="a"),
        steps=[TraversalStep(depth=3, direction=direction)],  # type: ignore[arg-type]
        ontology=ontology,
    )

    result = await DeterministicExecutor().execute(plan, store)

    hops = {hit.fact.id: hit.hops_from_seed for hit in result.hits}
    assert hops == {ab.id: 1, bc.id: 2, cd.id: 3}
    # Equal confidence: the nearer fact must outrank the farther one.
    assert [hit.fact.id for hit in result.hits] == [ab.id, bc.id, cd.id]


async def test_hops_accumulate_across_steps(ontology: Ontology) -> None:
    store, (ab, bc, cd) = await _chain()
    plan = Plan.build(
        seed=SeedByEntity(entity_id="a"),
        steps=[
            TraversalStep(depth=1, direction="out"),
            TraversalStep(depth=2, direction="out"),
        ],
        ontology=ontology,
    )

    result = await DeterministicExecutor().execute(plan, store)

    hops = {hit.fact.id: hit.hops_from_seed for hit in result.hits}
    assert hops == {ab.id: 1, bc.id: 2, cd.id: 3}
