"""Regression tests for GitHub issue #16.

The LLM planner can emit a `SeedByEntity.entity_id` that doesn't
literally match the id in the graph — most commonly by appending
`(<Type>)` (e.g. `"Alice Johnson (Person)"` when the store contains
`"Alice Johnson"`). Before the fix the executor did exact-string
identity check and silently returned zero hits.

These tests pin the resolution behavior:

  1. Exact match — unchanged, no warning.
  2. Type-suffix strip — resolves, emits a specific warning.
  3. Keyword-search fallback — resolves, emits a different warning.
  4. Genuinely unknown entity — empty result + `plan.seed produced
     no entities` warning (unchanged behavior for the real "not
     there" case).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology, load_ontology
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


@pytest.fixture
async def alice_store() -> NetworkxStore:
    """The exact repro shape from issue #16: `Alice Johnson --works_at--> Acme Corp`."""
    store = NetworkxStore()
    now = datetime.now(UTC)
    fact = Fact(
        subject_id="Alice Johnson",
        predicate="works_at",
        object_id="Acme Corp",
        provenance=Provenance(
            source_id="doc-1",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )
    await store.add_fact(fact)
    return store


async def test_exact_match_seed_still_works_without_warning(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits, "exact-match seed must still find the fact"
    assert any(
        h.fact.subject_id == "Alice Johnson" and h.fact.object_id == "Acme Corp"
        for h in result.hits
    )
    # No resolution warning should fire on an exact match.
    assert not any("resolved to" in w for w in result.warnings)


async def test_issue_16_type_suffix_seed_resolves_via_strip(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    """The exact scenario from GitHub issue #16."""
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson (Person)"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits, (
        "'Alice Johnson (Person)' must resolve to 'Alice Johnson' via "
        "type-suffix strip — this is the fix for issue #16"
    )
    assert any(
        h.fact.subject_id == "Alice Johnson" and h.fact.object_id == "Acme Corp"
        for h in result.hits
    )
    assert any(
        "resolved to 'Alice Johnson'" in w and "type-suffix strip" in w
        for w in result.warnings
    ), (
        "resolution must be visible in the ExecutionResult.warnings so "
        f"operators can see it happened; got warnings={result.warnings}"
    )


async def test_hyphenated_suffix_type_also_resolves(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    # Ontology labels can be single-word or CompoundWord; test the
    # underscore/hyphen path since LLMs occasionally follow either.
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson (Business-Person)"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)
    assert result.hits


async def test_keyword_fallback_when_entity_id_only_partially_matches(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    # No type suffix, but the id doesn't exactly match either
    # (planner produced a partial/lowercase form). Keyword search
    # should find the entity by substring.
    plan = Plan.build(
        seed=SeedByEntity(entity_id="alice johnson"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits, "case-mismatched seed should resolve via search fallback"
    assert any(
        "keyword search fallback" in w and "Alice Johnson" in w
        for w in result.warnings
    )


async def test_genuinely_unknown_entity_still_returns_empty_cleanly(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Nonexistent Person"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits == []
    assert any(
        "plan.seed produced no entities" in w for w in result.warnings
    ), (
        "an entity that genuinely isn't in the graph must still surface "
        "as an explicit warning, not a silent empty result"
    )


async def test_type_strip_wins_over_search_when_both_would_hit(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    # The suffix-strip path should take precedence over keyword search
    # — it's cheaper and more precise.
    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson (Person)"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)
    assert any("type-suffix strip" in w for w in result.warnings)
    assert not any("keyword search fallback" in w for w in result.warnings)


async def test_ambiguous_keyword_fallback_refuses_to_guess(
    ontology: Ontology,
) -> None:
    """Multiple substring-matching candidates → ambiguity warning, no resolution.

    Per @ALEKS0805's review on PR #20: silent wrong-entity resolution
    is worse than an explicit failure. If the search fallback finds
    "Acme Corp", "Acme Corp Europe", and "Acme Corp Holdings", the
    executor must refuse to pick one and instead surface the ambiguity.
    """
    store = NetworkxStore()
    now = datetime.now(UTC)

    def _mk(subject: str, obj: str) -> Fact:
        return Fact(
            subject_id=subject,
            predicate="works_at",
            object_id=obj,
            provenance=Provenance(
                source_id="doc",
                extractor_id="test",
                extractor_version="0.0.0",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=now,
            ingested_at=now,
        )

    # Three distinct entities all substring-matching "Acme Corp".
    await store.add_fact(_mk("Alice", "Acme Corp"))
    await store.add_fact(_mk("Bob", "Acme Corp Europe"))
    await store.add_fact(_mk("Carol", "Acme Corp Holdings"))

    plan = Plan.build(
        seed=SeedByEntity(entity_id="acme corp"),  # lowercase → forces search
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, store)

    assert result.hits == [], (
        "ambiguous search fallback must not silently pick one candidate"
    )
    ambiguity_warnings = [
        w for w in result.warnings if "matches multiple graph entities" in w
    ]
    assert ambiguity_warnings, (
        "an ambiguous search fallback must surface an explicit "
        f"ambiguity warning; got warnings={result.warnings}"
    )
    # The warning must name the candidates so the operator can pick one.
    warning_body = ambiguity_warnings[0]
    assert "Acme Corp" in warning_body
    assert "Acme Corp Europe" in warning_body
    assert "Acme Corp Holdings" in warning_body


async def test_unambiguous_keyword_fallback_still_resolves(
    alice_store: NetworkxStore, ontology: Ontology
) -> None:
    # Sanity check that the single-candidate path still resolves after
    # the ambiguity guard was added — regression backstop for the
    # already-passing `test_keyword_fallback_when_entity_id_only_partially_matches`
    # under the changed logic.
    plan = Plan.build(
        seed=SeedByEntity(entity_id="alice"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits, "single-candidate substring should still resolve"
    assert any("keyword search fallback" in w for w in result.warnings)


async def test_legitimate_name_with_parens_not_clobbered(
    ontology: Ontology,
) -> None:
    # If the store legitimately holds an entity id with a parenthesized
    # component, the exact-match path should return it before we even
    # try to strip anything. Guards against the fix over-cleaning
    # ids like "Meridian (US) Inc." — though our regex only strips
    # single identifier-shaped tokens, so this shouldn't strip anyway.
    store = NetworkxStore()
    now = datetime.now(UTC)
    fact = Fact(
        subject_id="Meridian (US) Inc.",
        predicate="works_at",
        object_id="Acme Corp",
        provenance=Provenance(
            source_id="doc-1",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
    )
    await store.add_fact(fact)

    plan = Plan.build(
        seed=SeedByEntity(entity_id="Meridian (US) Inc."),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, store)

    assert result.hits
    # No resolution warning — we found it exactly.
    assert not any("resolved to" in w for w in result.warnings)
