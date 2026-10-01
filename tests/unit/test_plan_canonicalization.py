"""Regression tests for GitHub issue #18.

`Plan.build()` drops consecutive-duplicate TraversalSteps while
preserving non-consecutive repeats. The LLM planner sometimes emits
`[step, step]` for what should be a single hop (the reporter's
scenario); `[a, b, a]` on the other hand is a legitimate 3-hop plan
(friend's employer's other friends) and must pass through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import Plan, SeedByEntity, SeedByKeyword, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


def test_issue_18_duplicate_subsidiary_of_steps_canonicalize_to_one(
    ontology: Ontology,
) -> None:
    """The exact scenario from GitHub issue #18."""
    dup = TraversalStep(relations=["subsidiary_of"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Beta Systems"),
        steps=[dup, dup],
        ontology=ontology,
    )
    assert len(plan.steps) == 1
    assert plan.steps[0] == dup


def test_single_step_passes_through_unchanged(ontology: Ontology) -> None:
    step = TraversalStep(relations=["works_at"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[step],
        ontology=ontology,
    )
    assert plan.steps == [step]


def test_empty_steps_stays_empty(ontology: Ontology) -> None:
    plan = Plan.build(
        seed=SeedByKeyword(keyword="Acme"),
        steps=[],
        ontology=ontology,
    )
    assert plan.steps == []


def test_non_adjacent_duplicates_preserved(ontology: Ontology) -> None:
    """`[a, b, a, c]` is a legitimate 4-hop; every step must survive."""
    a = TraversalStep(relations=["works_at"], depth=1, direction="out")
    b = TraversalStep(relations=["acquired"], depth=1, direction="out")
    c = TraversalStep(relations=["subsidiary_of"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[a, b, a, c],
        ontology=ontology,
    )
    assert plan.steps == [a, b, a, c]


def test_only_adjacent_runs_collapse(ontology: Ontology) -> None:
    """`[a, a, b, a]` collapses the first run but keeps the trailing `a`."""
    a = TraversalStep(relations=["works_at"], depth=1, direction="out")
    b = TraversalStep(relations=["acquired"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[a, a, b, a],
        ontology=ontology,
    )
    assert plan.steps == [a, b, a]


def test_adjacent_run_of_three_collapses_to_one(ontology: Ontology) -> None:
    """A run of N identical steps collapses to one, regardless of N."""
    a = TraversalStep(relations=["works_at"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[a, a, a],
        ontology=ontology,
    )
    assert plan.steps == [a]


def test_adjacent_steps_with_different_depth_both_kept(ontology: Ontology) -> None:
    """Same relations, different `depth` → distinct steps."""
    depth_one = TraversalStep(relations=["works_at"], depth=1, direction="out")
    depth_two = TraversalStep(relations=["works_at"], depth=2, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[depth_one, depth_two],
        ontology=ontology,
    )
    assert plan.steps == [depth_one, depth_two]


def test_adjacent_steps_with_different_direction_both_kept(ontology: Ontology) -> None:
    """Same relations + depth, different `direction` → distinct steps."""
    out_step = TraversalStep(relations=["works_at"], depth=1, direction="out")
    in_step = TraversalStep(relations=["works_at"], depth=1, direction="in")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[out_step, in_step],
        ontology=ontology,
    )
    assert plan.steps == [out_step, in_step]


def test_reordered_relations_list_not_collapsed(ontology: Ontology) -> None:
    """`relations=["a","b"]` vs `["b","a"]` stay distinct.

    Executor iterates `step.relations` in list order; silently
    reordering could mask real planner bugs.
    """
    ab = TraversalStep(relations=["works_at", "acquired"], depth=1, direction="out")
    ba = TraversalStep(relations=["acquired", "works_at"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[ab, ba],
        ontology=ontology,
    )
    assert plan.steps == [ab, ba]


def test_adjacent_wildcard_steps_collapse(ontology: Ontology) -> None:
    """Two consecutive identical empty-relations wildcard steps collapse."""
    wild = TraversalStep(relations=[], depth=1, direction="both")
    plan = Plan.build(
        seed=SeedByKeyword(keyword="Acme"),
        steps=[wild, wild],
        ontology=ontology,
    )
    assert plan.steps == [wild]


def test_canonicalization_does_not_hide_schema_violation(ontology: Ontology) -> None:
    """An invalid relation in a duplicate step still fails loud."""
    bad = TraversalStep(relations=["haunts"], depth=1, direction="out")
    with pytest.raises(ValueError, match="haunts"):
        Plan.build(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[bad, bad],
            ontology=ontology,
        )


# ────────────────────────────────────────────────────────────────────
# Executor-level semantics — proves non-adjacent repeats keep working
# ────────────────────────────────────────────────────────────────────


def _mk_fact(subject: str, predicate: str, obj: str) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="test",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def test_executor_non_adjacent_repeat_traverses_all_hops(
    ontology: Ontology,
) -> None:
    """`[acquired, subsidiary_of, acquired]` reaches the final node.

    Scenario (all outbound Company→Company edges, so works with the
    current executor+NetworkxStore combo):
        Alice Corp --acquired-----> Beta Corp
        Beta Corp  --subsidiary_of-> Gamma Corp
        Gamma Corp --acquired-----> Delta Corp

    A 3-hop plan `[acquired, subsidiary_of, acquired]` must walk the
    full chain and reach Delta Corp. If the previous global dedupe
    were still firing, step 3 (`acquired`) would be collapsed into
    step 1 and Delta Corp would not be reached — this test fails
    loud if non-adjacent repeats are ever dropped again.
    """
    store = NetworkxStore()
    await store.add_fact(_mk_fact("Alice Corp", "acquired", "Beta Corp"))
    await store.add_fact(_mk_fact("Beta Corp", "subsidiary_of", "Gamma Corp"))
    await store.add_fact(_mk_fact("Gamma Corp", "acquired", "Delta Corp"))

    step_acquired = TraversalStep(relations=["acquired"], depth=1, direction="out")
    step_subsidiary = TraversalStep(relations=["subsidiary_of"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Corp"),
        steps=[step_acquired, step_subsidiary, step_acquired],
        ontology=ontology,
    )
    # The third step must survive the dedupe — it is non-adjacent to
    # the first `acquired`.
    assert plan.steps == [step_acquired, step_subsidiary, step_acquired]

    result = await DeterministicExecutor().execute(plan, store)
    reached_entities: set[str] = set()
    for hit in result.hits:
        reached_entities.add(hit.fact.subject_id)
        reached_entities.add(hit.fact.object_id)
    assert "Delta Corp" in reached_entities, (
        f"3-hop traversal should reach Delta Corp via the final `acquired` "
        f"hop; reached={reached_entities}, warnings={result.warnings}"
    )
