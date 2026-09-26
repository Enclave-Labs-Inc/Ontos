"""Planner Protocol — the Scribe-drop-in boundary for query planning.

The Planner takes a natural-language question and an Ontology, and
returns a typed `Plan` the executor can run deterministically.
Nothing downstream of `ontos.planner` should import LLM-specific
types past this file.

Design parallels `ontos.extraction.base.Extractor`:
- Any provider adapts to a narrow `LLMBackend` surface (reused from
  `ontos.extraction.llm_extractor`).
- Real LLM integration is exercised manually; tests use a
  deterministic fake.
- Planner failures raise `PlannerError` — silent-empty plans are
  never returned.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ontos.ontology import Ontology
from ontos.planner.plan import Plan


@runtime_checkable
class Planner(Protocol):
    """The interface every planner satisfies."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def plan(self, question: str, ontology: Ontology) -> Plan: ...


class PlannerError(RuntimeError):
    """Raised when the planner cannot produce a valid Plan.

    The planner MUST NOT return an empty or malformed Plan to the
    executor; that would violate the schema-strict contract. Instead
    surface a PlannerError so the caller can decide (fail the query,
    fall back to a heuristic, etc.).
    """

    def __init__(self, question: str, message: str) -> None:
        self.question = question[:500]
        super().__init__(f"planner failed on question={self.question!r}: {message}")
