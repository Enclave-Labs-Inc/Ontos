"""Reusable benchmark harness — non-gate, regression-tracking only.

Given a graded question set (question + expected fact ids), runs each
question through a supplied `ask` callable and reports recall@k,
precision, and latency percentiles.

Deliberately does NOT gate CI on absolute numbers — LLM-driven
retrieval quality drifts on model updates, and gating on drift would
either freeze the stack or produce noisy CI. What CI DOES gate on: a
regression that produces a Python error, an empty result set on a
question that used to have hits, or a >2× latency spike.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GradedQuestion:
    question: str
    expected_fact_ids: frozenset[str]


@dataclass(frozen=True)
class BenchmarkResult:
    total_questions: int
    mean_recall_at_k: float
    mean_precision_at_k: float
    latency_p50_ms: float
    latency_p95_ms: float
    per_question_error_count: int

    def formatted(self) -> str:
        return (
            f"n={self.total_questions}  "
            f"recall@k={self.mean_recall_at_k:.3f}  "
            f"precision@k={self.mean_precision_at_k:.3f}  "
            f"p50={self.latency_p50_ms:.1f}ms  "
            f"p95={self.latency_p95_ms:.1f}ms  "
            f"errors={self.per_question_error_count}"
        )


AskFn = Callable[[str], Awaitable[list[str]]]
"""A test's `ask` implementation. Takes a question, returns the list of
returned fact ids in ranked order. Callers wrap whatever is under test
(the MCP `ask` tool, the executor directly, an alternative planner) to
match this shape."""


async def run_benchmark(
    ask: AskFn,
    questions: list[GradedQuestion],
    *,
    k: int = 10,
) -> BenchmarkResult:
    """Run every question, compute per-question recall/precision at k, aggregate.

    Failures (exceptions raised by `ask`) count in `per_question_error_count`
    and score 0 for recall/precision on that question.
    """
    recalls: list[float] = []
    precisions: list[float] = []
    latencies_ms: list[float] = []
    errors = 0

    for q in questions:
        t0 = time.perf_counter()
        try:
            returned = await ask(q.question)
        except Exception:  # noqa: BLE001 — benchmark path swallows for measurement
            errors += 1
            latencies_ms.append((time.perf_counter() - t0) * 1000)
            recalls.append(0.0)
            precisions.append(0.0)
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)

        top_k = set(returned[:k])
        expected = q.expected_fact_ids
        if not expected:
            # Question with no expected hits — recall undefined; skip from means.
            continue
        hit_count = len(top_k & expected)
        recalls.append(hit_count / len(expected))
        precisions.append(hit_count / max(len(top_k), 1))

    return BenchmarkResult(
        total_questions=len(questions),
        mean_recall_at_k=statistics.fmean(recalls) if recalls else 0.0,
        mean_precision_at_k=statistics.fmean(precisions) if precisions else 0.0,
        latency_p50_ms=statistics.median(latencies_ms) if latencies_ms else 0.0,
        latency_p95_ms=_percentile(latencies_ms, 0.95),
        per_question_error_count=errors,
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(len(ordered) * p)))
    return ordered[k]
