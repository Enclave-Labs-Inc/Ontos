"""M3.a — Plan model + Planner Protocol tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.ontology import Ontology, load_ontology
from ontos.planner import (
    LlmPlanner,
    Plan,
    Planner,
    PlannerError,
    RawPlan,
    SeedByEntity,
    SeedByKeyword,
    TraversalStep,
)

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


class FakePlannerBackend:
    id: str = "fake-planner-llm"
    version: str = "test-1"

    def __init__(self, plans: list[RawPlan | Exception]) -> None:
        self._plans = list(plans)

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan:
        if not self._plans:
            raise AssertionError("FakePlannerBackend ran out of programmed plans")
        p = self._plans.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def test_seed_variants_are_frozen() -> None:
    import pydantic

    seed = SeedByEntity(entity_id="e:1")
    with pytest.raises(pydantic.ValidationError):
        seed.entity_id = "e:2"  # type: ignore[misc]


def test_plan_build_accepts_valid_relations(ontology: Ontology) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(relations=["works_at"], depth=1)],
        ontology=ontology,
    )
    assert plan.total_depth() == 1


def test_plan_build_rejects_undeclared_relation(ontology: Ontology) -> None:
    with pytest.raises(ValueError, match="haunts"):
        Plan.build(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[TraversalStep(relations=["haunts"], depth=1)],
            ontology=ontology,
        )


def test_plan_build_allows_empty_relations_meaning_any(ontology: Ontology) -> None:
    plan = Plan.build(
        seed=SeedByKeyword(keyword="Acme"),
        steps=[TraversalStep(relations=[], depth=2)],
        ontology=ontology,
    )
    assert plan.steps[0].relations == []


def test_traversal_step_depth_capped_by_pydantic() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TraversalStep(depth=99)


def test_plan_limit_bounded_by_pydantic() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Plan(seed=SeedByEntity(entity_id="e"), steps=[], limit=99999)


def test_seed_discriminator_routes_by_kind() -> None:
    plan = Plan.model_validate(
        {
            "seed": {"kind": "by_keyword", "keyword": "acme", "k": 3},
            "steps": [],
        }
    )
    assert isinstance(plan.seed, SeedByKeyword)
    assert plan.seed.k == 3


def test_llm_planner_conforms_to_protocol(ontology: Ontology) -> None:
    planner = LlmPlanner(FakePlannerBackend([]), ontology)
    assert isinstance(planner, Planner)


def test_llm_planner_id_wraps_backend_id(ontology: Ontology) -> None:
    planner = LlmPlanner(FakePlannerBackend([]), ontology)
    assert "fake-planner-llm" in planner.id
    assert planner.version == "test-1"


async def test_llm_planner_returns_validated_plan(ontology: Ontology) -> None:
    raw = RawPlan(
        seed=SeedByEntity(entity_id="person:alice"),
        steps=[TraversalStep(relations=["works_at"], depth=1)],
        limit=5,
    )
    planner = LlmPlanner(FakePlannerBackend([raw]), ontology)
    plan = await planner.plan("Where does Alice work?", ontology)
    assert plan.limit == 5
    assert isinstance(plan.seed, SeedByEntity)


async def test_llm_planner_raises_on_empty_question(ontology: Ontology) -> None:
    planner = LlmPlanner(FakePlannerBackend([]), ontology)
    with pytest.raises(PlannerError, match="empty question"):
        await planner.plan("   ", ontology)


async def test_llm_planner_wraps_backend_exception_as_planner_error(
    ontology: Ontology,
) -> None:
    class BoomError(RuntimeError):
        pass

    planner = LlmPlanner(FakePlannerBackend([BoomError("API down")]), ontology)
    with pytest.raises(PlannerError, match="LLM backend failed"):
        await planner.plan("anything", ontology)


async def test_llm_planner_rejects_schema_violation_from_backend(
    ontology: Ontology,
) -> None:
    raw = RawPlan(
        seed=SeedByEntity(entity_id="e"),
        steps=[TraversalStep(relations=["nonexistent_relation"], depth=1)],
    )
    planner = LlmPlanner(FakePlannerBackend([raw]), ontology)
    with pytest.raises(PlannerError, match="ontology validation failed"):
        await planner.plan("q", ontology)


def test_planner_error_carries_question_prefix() -> None:
    err = PlannerError("a" * 1000, "x")
    assert len(err.question) == 500
    assert "planner failed" in str(err)
