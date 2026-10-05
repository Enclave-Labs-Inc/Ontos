"""LLM-backed planner.

Takes an `LLMBackend` (the same seam as extraction; any provider adapts
to it) and an `Ontology`, and asks the LLM for a structured plan. The
LLM returns a JSON-serializable `RawPlan`; we validate + reify into a
`Plan` against the ontology so schema violations fail before hitting
the executor.

The `LLMBackend` here is a Protocol independent from
`ontos.extraction`'s — the shape they need is different (extraction
returns triples; planning returns a plan). Sharing an adapter is up
to the concrete provider.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ontos.ontology import Ontology
from ontos.planner.base import PlannerError
from ontos.planner.plan import Plan, Seed, SeedByEntity, SeedByKeyword, TraversalStep

log = structlog.get_logger()


class RawPlan(BaseModel):
    """What an LLMPlannerBackend returns before ontology validation."""

    seed: Seed = Field(discriminator="kind")
    steps: list[TraversalStep] = Field(default_factory=list)
    limit: int = 20

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class LLMPlannerBackend(Protocol):
    """Narrow surface for LLM-backed planning."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan: ...


class LlmPlanner:
    """Planner backed by an `LLMPlannerBackend`.

    Delegates the actual LLM call to the backend; owns the validation
    against the ontology and the loud-fail contract.
    """

    def __init__(self, llm: LLMPlannerBackend, ontology: Ontology) -> None:
        self._llm = llm
        self._ontology = ontology

    @property
    def id(self) -> str:
        return f"ontos.llm_planner+{self._llm.id}"

    @property
    def version(self) -> str:
        return self._llm.version

    async def plan(self, question: str, ontology: Ontology) -> Plan:
        if not question or not question.strip():
            raise PlannerError(question, "empty question")

        try:
            raw = await self._llm.structured_plan(question, ontology)
        except PlannerError:
            raise
        except Exception as exc:  # noqa: BLE001 — see PlannerError contract
            # Deferred import keeps the planner's Scribe-drop-in boundary
            # free of Ollama coupling at construction time; this is
            # Ollama-specific message sugar, not a planner dependency.
            from ontos.llm.ollama import OllamaTimeoutError

            if isinstance(exc, OllamaTimeoutError):
                raise PlannerError(question, f"planner LLM timed out: {exc}") from exc
            raise PlannerError(question, f"LLM backend failed: {exc}") from exc

        try:
            plan = Plan.build(
                seed=raw.seed,
                steps=raw.steps,
                limit=raw.limit,
                ontology=ontology,
            )
        except (ValueError, Exception) as exc:
            raise PlannerError(question, f"ontology validation failed: {exc}") from exc

        # Surface planner drift so operators notice when the LLM
        # emits consecutive-identical steps (which `Plan.build` drops).
        if len(plan.steps) < len(raw.steps):
            log.warning(
                "planner emitted duplicate steps",
                planner_id=self.id,
                raw_step_count=len(raw.steps),
                canonical_step_count=len(plan.steps),
            )

        return plan


__all__ = [
    "LLMPlannerBackend",
    "LlmPlanner",
    "RawPlan",
    "Seed",
    "SeedByEntity",
    "SeedByKeyword",
    "TraversalStep",
]
