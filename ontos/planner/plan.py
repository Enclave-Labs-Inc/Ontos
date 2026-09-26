"""Typed Plan — the interface between the LLM planner and the deterministic executor.

A `Plan` is what the LLM emits and what the executor consumes. Frozen
Pydantic; validated against the `Ontology` at construction time so
schema violations fail loud at plan-build, not deep inside the
executor. This is the direct implementation of CypherBench's
"schema-constrained decoding" recommendation — the plan schema
narrows the space the LLM can emit into.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ontos.ontology import Ontology


class SeedByEntity(BaseModel):
    """Start traversal from a specific entity id."""

    kind: Literal["by_entity"] = "by_entity"
    entity_id: str

    model_config = ConfigDict(frozen=True)


class SeedByKeyword(BaseModel):
    """Start traversal from entities matched by a keyword search."""

    kind: Literal["by_keyword"] = "by_keyword"
    keyword: str
    k: int = Field(default=5, ge=1, le=100)

    model_config = ConfigDict(frozen=True)


Seed = SeedByEntity | SeedByKeyword


class TraversalStep(BaseModel):
    """One hop expansion.

    `relations` empty = any relation; otherwise every string must be a
    declared relation type in the ontology (validated at Plan build).
    `depth` capped defensively; the executor caps again at its
    boundary.
    """

    relations: list[str] = Field(default_factory=list)
    depth: int = Field(default=1, ge=1, le=5)
    direction: Literal["out", "in", "both"] = "both"

    model_config = ConfigDict(frozen=True)


class Plan(BaseModel):
    """A validated execution plan produced by a Planner.

    Construction goes through `Plan.build(...)` — the constructor is a
    Pydantic default and doesn't cross-validate against an ontology.
    Callers should not bypass `build()`.
    """

    seed: Seed = Field(discriminator="kind")
    steps: list[TraversalStep] = Field(default_factory=list, max_length=8)
    as_of: datetime | None = None
    limit: int = Field(default=20, ge=1, le=200)

    model_config = ConfigDict(frozen=True)

    @classmethod
    def build(
        cls,
        *,
        seed: Seed,
        steps: list[TraversalStep] | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
        ontology: Ontology,
    ) -> Plan:
        """Construct a Plan and validate every referenced relation
        against the ontology.

        A referenced relation that's not declared is a schema
        violation — fail loud here, not in the executor.
        """
        actual_steps = list(steps or [])
        declared_relations = {rt.label for rt in ontology.relation_types}
        for i, step in enumerate(actual_steps):
            for rel in step.relations:
                if rel not in declared_relations:
                    raise ValueError(
                        f"plan step[{i}] references relation {rel!r} "
                        "which is not declared in the ontology"
                    )
        return cls(seed=seed, steps=actual_steps, as_of=as_of, limit=limit)

    def total_depth(self) -> int:
        return sum(step.depth for step in self.steps)
