"""Regression tests for GitHub issue #18.

The LLM planner can non-deterministically emit the same TraversalStep
twice for a single-hop query. `Plan.build()` now canonicalizes `steps`
by dropping exact-duplicate steps while preserving first-occurrence
order, so the executor stops making redundant store calls and the
Article-12 audit record carries the canonical plan the executor
actually ran — not an inflated LLM dump.

These tests pin the canonicalization semantics:
    1. Issue #18 exact repro — two identical subsidiary_of steps → one.
    2. Order-preserving dedupe across non-adjacent duplicates.
    3. Exact-key only — different depth / direction / relation-order
       stays distinct (silently merging those would mask real bugs).
    4. Edge cases (empty steps, single step, empty-relations wildcard).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.ontology import Ontology, load_ontology
from ontos.planner import Plan, SeedByEntity, SeedByKeyword, TraversalStep

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
    assert len(plan.steps) == 1, (
        f"issue #18: duplicate steps must canonicalize to one; got {len(plan.steps)}"
    )
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


def test_non_adjacent_duplicates_also_deduped(ontology: Ontology) -> None:
    """`[a, b, a, c]` must canonicalize to `[a, b, c]` with order preserved."""
    a = TraversalStep(relations=["works_at"], depth=1, direction="out")
    b = TraversalStep(relations=["acquired"], depth=1, direction="out")
    c = TraversalStep(relations=["subsidiary_of"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[a, b, a, c],
        ontology=ontology,
    )
    assert plan.steps == [a, b, c]


def test_first_occurrence_order_preserved(ontology: Ontology) -> None:
    """Dedupe keeps the FIRST sighting of a step; audit stays stable."""
    first = TraversalStep(relations=["works_at"], depth=1, direction="out")
    second = TraversalStep(relations=["acquired"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[first, second, first],
        ontology=ontology,
    )
    assert plan.steps[0] == first
    assert plan.steps[1] == second
    assert len(plan.steps) == 2


def test_steps_with_different_depth_not_deduped(ontology: Ontology) -> None:
    """Same relations, different `depth` → distinct steps."""
    depth_one = TraversalStep(relations=["works_at"], depth=1, direction="out")
    depth_two = TraversalStep(relations=["works_at"], depth=2, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[depth_one, depth_two],
        ontology=ontology,
    )
    assert len(plan.steps) == 2


def test_steps_with_different_direction_not_deduped(ontology: Ontology) -> None:
    """Same relations + depth, different `direction` → distinct steps."""
    out_step = TraversalStep(relations=["works_at"], depth=1, direction="out")
    in_step = TraversalStep(relations=["works_at"], depth=1, direction="in")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[out_step, in_step],
        ontology=ontology,
    )
    assert len(plan.steps) == 2


def test_reordered_relations_list_not_deduped(ontology: Ontology) -> None:
    """`relations=["a","b"]` vs `["b","a"]` stay distinct.

    Executor iterates `step.relations` in list order, so a silent
    reorder-aware merge would mask real planner bugs. Scope decision
    from the plan: exact-key dedupe only.
    """
    ab = TraversalStep(relations=["works_at", "acquired"], depth=1, direction="out")
    ba = TraversalStep(relations=["acquired", "works_at"], depth=1, direction="out")
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[ab, ba],
        ontology=ontology,
    )
    assert len(plan.steps) == 2


def test_wildcard_steps_dedupe_too(ontology: Ontology) -> None:
    """Two identical empty-relations wildcard steps canonicalize to one."""
    wild = TraversalStep(relations=[], depth=1, direction="both")
    plan = Plan.build(
        seed=SeedByKeyword(keyword="Acme"),
        steps=[wild, wild],
        ontology=ontology,
    )
    assert len(plan.steps) == 1


def test_canonicalization_does_not_hide_schema_violation(ontology: Ontology) -> None:
    """Dedupe runs AFTER relation validation — an invalid relation
    in a duplicate step still fails loud, as before."""
    bad = TraversalStep(relations=["haunts"], depth=1, direction="out")
    with pytest.raises(ValueError, match="haunts"):
        Plan.build(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[bad, bad],
            ontology=ontology,
        )
