"""Structured-output response models shared by the hosted LLM adapters.

The OpenAI and Anthropic adapters both constrain completions to these
Pydantic shapes, then reify plans into the planner's typed `RawPlan`.
Keeping them in one place means both adapters accept and reject
exactly the same model output.
"""

from __future__ import annotations

from pydantic import BaseModel

from ontos.extraction import RawTriple
from ontos.planner import (
    RawPlan,
    Seed,
    SeedByEntity,
    SeedByKeyword,
    TraversalStep,
)


class _ExtractionResponse(BaseModel):
    triples: list[RawTriple]


class _PlanSeed(BaseModel):
    kind: str
    entity_id: str | None = None
    keyword: str | None = None
    k: int | None = None


class _PlanStep(BaseModel):
    relations: list[str] = []
    depth: int = 1
    direction: str = "both"


class _PlanResponse(BaseModel):
    seed: _PlanSeed
    steps: list[_PlanStep] = []
    limit: int = 20


def _reify_plan(response: _PlanResponse) -> RawPlan:
    seed: Seed
    if response.seed.kind == "by_entity":
        if not response.seed.entity_id:
            raise ValueError("by_entity seed requires entity_id")
        seed = SeedByEntity(entity_id=response.seed.entity_id)
    elif response.seed.kind == "by_keyword":
        if not response.seed.keyword:
            raise ValueError("by_keyword seed requires keyword")
        seed = SeedByKeyword(keyword=response.seed.keyword, k=response.seed.k or 5)
    else:
        raise ValueError(f"unknown seed kind: {response.seed.kind}")
    steps = [
        TraversalStep(
            relations=step.relations,
            depth=step.depth,
            direction=step.direction,  # type: ignore[arg-type]
        )
        for step in response.steps
    ]
    return RawPlan(seed=seed, steps=steps, limit=response.limit)
