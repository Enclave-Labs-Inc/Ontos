"""Graded-question data model shared across verticals.

Kept intentionally small — the shape has to survive the ontology
edits + corpus edits between here and the live-execution runner
that lands in a later PR. Anything you add here has to earn its
seat by carrying real signal at *both* static-validation time and
live-execution time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Difficulty = Literal[
    "single_hop",
    "multi_hop",
    "cross_source",
    "temporal",
    "provenance",
]


class Hop(BaseModel):
    """One edge of the expected traversal path over the ontology."""

    subject_type: str
    predicate: str
    object_type: str

    model_config = ConfigDict(frozen=True)


class GradedQuestion(BaseModel):
    """A graded natural-language question with an expected retrieval shape.

    - `hops` — the multi-hop path the planner should emit. Every hop
      is checked against `Ontology.is_valid_pattern(...)`; a stale
      hop causes the static test to fail loudly.
    - `expected_source_docs` — filenames (relative to the vertical's
      corpus dir) that the provenance chain must cite for the
      answer to be considered correct. Missing files fail loudly.
    - `expected_entities` — strings that the natural-language answer
      is expected to include. Each string must appear in at least
      one `expected_source_docs` file (proves the question is
      realizable — the extractor can't return what the corpus
      doesn't contain).
    - `as_of` — ISO-8601 date; when set, the executor should apply
      this as the bitemporal query timestamp before returning.
    """

    id: str
    question: str
    difficulty: Difficulty
    hops: list[Hop] = Field(default_factory=list)
    expected_source_docs: list[str]
    expected_entities: list[str]
    as_of: str | None = None
    notes: str = ""

    model_config = ConfigDict(frozen=True)
