"""Synthetic benchmark exercising the M3 executor against NetworkxStore.

Non-gate. Marked `benchmark`; excluded from default CI.

What the harness proves here (deliberately modest):
- Deterministic executor doesn't crash on the synthetic corpus.
- Recall > 0 on questions with obvious answers.
- Latency stays well under 500ms per question on this tiny corpus.

Real benchmarks against LOCOMO / HotpotQA / CypherBench land as
follow-ups once we have a real LLM planner + real corpora.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore
from tests.benchmarks.harness import GradedQuestion, run_benchmark

pytestmark = pytest.mark.benchmark


def _fact(subject: str, predicate: str, obj: str) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="benchmark",
            extractor_id="synthetic",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def _seed_graph() -> tuple[NetworkxStore, dict[str, str]]:
    store = NetworkxStore()
    fact_ids: dict[str, str] = {}
    for subject, predicate, obj in [
        ("person:alice", "works_at", "company:acme"),
        ("company:acme", "acquired", "company:widget"),
        ("company:widget", "subsidiary_of", "company:foo"),
        ("person:bob", "works_at", "company:distractor"),
    ]:
        f = _fact(subject, predicate, obj)
        await store.add_fact(f)
        fact_ids[f"{subject}-{predicate}-{obj}"] = str(f.id)
    return store, fact_ids


@pytest.fixture
def ontology() -> Ontology:
    from pathlib import Path

    return load_ontology(
        Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
    )


async def test_synthetic_benchmark_runs_and_finds_seed_neighbors(
    ontology: Ontology,
) -> None:
    store, fact_ids = await _seed_graph()

    async def ask(question: str) -> list[str]:
        # Toy planner: one entity seed + 3-hop expansion.
        plan = Plan.build(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[TraversalStep(depth=3)],
            ontology=ontology,
        )
        result = await DeterministicExecutor().execute(plan, store)
        return [str(h.fact.id) for h in result.hits]

    questions = [
        GradedQuestion(
            question="What does Alice do at Acme?",
            expected_fact_ids=frozenset({fact_ids["person:alice-works_at-company:acme"]}),
        ),
        GradedQuestion(
            question="Where did Acme acquire?",
            expected_fact_ids=frozenset({fact_ids["company:acme-acquired-company:widget"]}),
        ),
    ]
    result = await run_benchmark(ask, questions, k=5)

    # Regression guards (not quality gates):
    assert result.total_questions == 2
    assert result.per_question_error_count == 0
    assert result.mean_recall_at_k > 0.0
    assert result.latency_p95_ms < 500.0
    # Should be able to print without exploding.
    assert isinstance(result.formatted(), str)


async def test_benchmark_counts_errors_without_crashing() -> None:
    async def broken_ask(question: str) -> list[str]:
        raise RuntimeError("simulated planner outage")

    questions = [
        GradedQuestion(question="x", expected_fact_ids=frozenset({"f1"})),
        GradedQuestion(question="y", expected_fact_ids=frozenset({"f2"})),
    ]
    result = await run_benchmark(broken_ask, questions)
    assert result.per_question_error_count == 2
    assert result.mean_recall_at_k == 0.0


async def test_benchmark_ignores_questions_with_no_expected_hits() -> None:
    async def ask(_: str) -> list[str]:
        return ["random"]

    questions = [
        GradedQuestion(question="a", expected_fact_ids=frozenset()),
    ]
    result = await run_benchmark(ask, questions)
    assert result.total_questions == 1
    assert result.mean_recall_at_k == 0.0  # empty means → 0 by convention
