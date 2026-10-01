"""M7.d — static validation of the vertical graded question suites.

These tests prove each question is ontology-valid and corpus-realizable
before we can run the live pipeline against them (planner + executor
+ store + LLM extractor — that layer lands behind a `graded` pytest
marker in a later PR).

For every question in `_fintech.py` and `_pharma.py`, we check:

- every hop is a `Pattern` declared in the vertical's ontology;
- every `expected_source_docs` filename exists in the vertical's
  corpus directory;
- every `expected_entities` string appears (case-insensitive) in at
  least one of the question's expected source documents;
- `as_of` (when set) parses as an ISO-8601 date.

Plus per-suite invariants — id uniqueness, question count, tier
distribution — so a well-meaning edit that shrinks a tier fails loud.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ontos.ontology import Ontology, load_ontology
from tests.verticals._fintech import FINTECH_QUESTIONS
from tests.verticals._pharma import PHARMA_QUESTIONS
from tests.verticals._schema import GradedQuestion

REPO_ROOT = Path(__file__).resolve().parents[2]

FINTECH_ONT = load_ontology(REPO_ROOT / "docs" / "ontology" / "examples" / "fintech.yaml")
PHARMA_ONT = load_ontology(REPO_ROOT / "docs" / "ontology" / "examples" / "pharma.yaml")
FINTECH_CORPUS = REPO_ROOT / "docs" / "ontology" / "examples" / "fintech-corpus"
PHARMA_CORPUS = REPO_ROOT / "docs" / "ontology" / "examples" / "pharma-corpus"


VERTICALS: dict[str, tuple[Ontology, Path, list[GradedQuestion]]] = {
    "fintech": (FINTECH_ONT, FINTECH_CORPUS, FINTECH_QUESTIONS),
    "pharma": (PHARMA_ONT, PHARMA_CORPUS, PHARMA_QUESTIONS),
}

EXPECTED_QUESTION_COUNT = 20
EXPECTED_TIER_DISTRIBUTION: dict[str, int] = {
    "single_hop": 5,
    "multi_hop": 7,
    "cross_source": 4,
    "temporal": 2,
    "provenance": 2,
}


# --------------------------------------------------------------------
# Per-suite invariants
# --------------------------------------------------------------------


@pytest.mark.parametrize("vertical", ["fintech", "pharma"])
def test_suite_has_exactly_twenty_questions(vertical: str) -> None:
    _, _, questions = VERTICALS[vertical]
    assert len(questions) == EXPECTED_QUESTION_COUNT


@pytest.mark.parametrize("vertical", ["fintech", "pharma"])
def test_suite_ids_are_unique(vertical: str) -> None:
    _, _, questions = VERTICALS[vertical]
    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids)), f"{vertical}: duplicate question ids"


@pytest.mark.parametrize("vertical", ["fintech", "pharma"])
def test_suite_hits_target_tier_distribution(vertical: str) -> None:
    # The tier mix is load-bearing — we want a fixed floor of coverage
    # per difficulty. If someone drops all provenance questions, the
    # graded suite silently stops being useful for our compliance pitch.
    _, _, questions = VERTICALS[vertical]
    actual: dict[str, int] = {}
    for q in questions:
        actual[q.difficulty] = actual.get(q.difficulty, 0) + 1
    for tier, expected in EXPECTED_TIER_DISTRIBUTION.items():
        assert actual.get(tier, 0) == expected, (
            f"{vertical}: tier {tier!r} has {actual.get(tier, 0)}, "
            f"expected {expected}"
        )


# --------------------------------------------------------------------
# Per-question checks (parametrized across every graded question)
# --------------------------------------------------------------------

_ALL_QUESTIONS: list[tuple[str, GradedQuestion]] = [
    *(("fintech", q) for q in FINTECH_QUESTIONS),
    *(("pharma", q) for q in PHARMA_QUESTIONS),
]


def _idfn(param: tuple[str, GradedQuestion]) -> str:
    return param[1].id


@pytest.mark.parametrize("vertical_question", _ALL_QUESTIONS, ids=_idfn)
def test_hops_valid_against_ontology(
    vertical_question: tuple[str, GradedQuestion],
) -> None:
    vertical, q = vertical_question
    ontology, _, _ = VERTICALS[vertical]
    for hop in q.hops:
        assert ontology.is_valid_pattern(
            hop.subject_type, hop.predicate, hop.object_type
        ), (
            f"{q.id}: hop {hop.subject_type} --{hop.predicate}--> "
            f"{hop.object_type} is not a declared pattern in the "
            f"{vertical} ontology"
        )


@pytest.mark.parametrize("vertical_question", _ALL_QUESTIONS, ids=_idfn)
def test_expected_source_docs_exist(
    vertical_question: tuple[str, GradedQuestion],
) -> None:
    vertical, q = vertical_question
    _, corpus_dir, _ = VERTICALS[vertical]
    assert q.expected_source_docs, f"{q.id}: no expected_source_docs"
    for filename in q.expected_source_docs:
        assert (corpus_dir / filename).is_file(), (
            f"{q.id}: expected source doc {filename!r} not present in "
            f"{corpus_dir.relative_to(REPO_ROOT)}"
        )


@pytest.mark.parametrize("vertical_question", _ALL_QUESTIONS, ids=_idfn)
def test_expected_entities_present_in_expected_docs(
    vertical_question: tuple[str, GradedQuestion],
) -> None:
    """Each expected entity must appear in at least one expected source doc.

    This proves the question is *realizable* — the extractor can't be
    graded on returning content the corpus doesn't contain. Match is
    case-insensitive so we don't have to worry about "SR 11-7" vs
    "sr 11-7" style casing drift.
    """
    vertical, q = vertical_question
    _, corpus_dir, _ = VERTICALS[vertical]

    texts_lower = [
        (corpus_dir / name).read_text().lower() for name in q.expected_source_docs
    ]
    for entity in q.expected_entities:
        needle = entity.lower()
        assert any(needle in text for text in texts_lower), (
            f"{q.id}: expected entity {entity!r} not found in any of the "
            f"expected_source_docs {q.expected_source_docs}"
        )


@pytest.mark.parametrize("vertical_question", _ALL_QUESTIONS, ids=_idfn)
def test_as_of_parses_as_iso_date_when_set(
    vertical_question: tuple[str, GradedQuestion],
) -> None:
    _, q = vertical_question
    if q.as_of is None:
        return
    # date.fromisoformat is strict on YYYY-MM-DD; a garbled date fails here.
    parsed = date.fromisoformat(q.as_of)
    assert isinstance(parsed, date)


# --------------------------------------------------------------------
# Global cross-suite invariants
# --------------------------------------------------------------------


def test_no_id_collision_across_suites() -> None:
    all_ids = [q.id for _, q in _ALL_QUESTIONS]
    assert len(all_ids) == len(set(all_ids)), "duplicate ids across verticals"


def test_every_id_carries_vertical_prefix() -> None:
    # Discipline — reading a failure like `test_hops_valid[fintech-q06]`
    # should immediately tell you the vertical without opening the file.
    for vertical, q in _ALL_QUESTIONS:
        assert q.id.startswith(f"{vertical}-"), (
            f"{q.id}: id should start with {vertical!r}"
        )
