"""Executor Protocol + result types.

The Executor consumes a validated `Plan` (from `ontos.planner`) and
runs deterministic multi-hop retrieval against a `GraphStore`.
Ranking combines Personalized-PageRank scores (HippoRAG-style joint
seed activation) with path-flow scores (PathRAG-style, decay α, early
stop θ).

The executor never calls an LLM. Everything below the Planner is
deterministic — same plan + same store = same ranked output.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ontos.planner import Plan
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore


class ExecutionHit(BaseModel):
    """One ranked result from executing a Plan.

    `ppr_score` is the Personalized-PageRank weight on the fact's
    source or target node (whichever is higher). `path_score` is the
    PathRAG-style flow score along the path that reached this fact.
    `combined_score` is the executor's chosen mix; callers rank on it.
    """

    fact: Fact
    combined_score: float = Field(ge=0.0)
    ppr_score: float = Field(ge=0.0)
    path_score: float = Field(ge=0.0)
    hops_from_seed: int = Field(ge=0)

    model_config = ConfigDict(frozen=True)


class ExecutionResult(BaseModel):
    """Executor output. `hits` is already ranked highest-first."""

    hits: list[ExecutionHit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class Executor(Protocol):
    """Every executor implementation satisfies this."""

    @property
    def id(self) -> str: ...

    async def execute(
        self,
        plan: Plan,
        store: GraphStore,
        *,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> ExecutionResult: ...
