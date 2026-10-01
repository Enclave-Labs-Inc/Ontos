"""Compliance test — seed resolution must not leak forbidden-entity existence.

The seed-resolution fallback added for issue #16 echoes candidate ids
into `ExecutionResult.warnings` (`"resolved to 'Alice Johnson' via ..."`).
If `store.facts_for_entity` / `store.search` were called without
forwarding the caller's ACL context, those warnings would betray the
existence of a forbidden entity even though the result set itself was
empty — a classic node-existence leak forbidden by permission-aware
traversal.

This test pins the invariant: when the only entity matching a seed is
behind an ACL the caller cannot see, the result and the warnings must
be identical to the "unknown entity" case, with the forbidden entity's
id appearing nowhere in the warnings.

Requested by the reviewer on PR #20.
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
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)

_CONFIDENTIAL = "acl:confidential"
_FORBIDDEN_SUBJECT = "Classified Project Zephyr"
_FORBIDDEN_OBJECT = "Underground Research Facility"


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


@pytest.fixture
async def confidential_only_store() -> NetworkxStore:
    """A store where every fact about the target entity sits behind a forbidden ACL."""
    store = NetworkxStore()
    now = datetime.now(UTC)
    fact = Fact(
        subject_id=_FORBIDDEN_SUBJECT,
        predicate="works_at",
        object_id=_FORBIDDEN_OBJECT,
        provenance=Provenance(
            source_id="classified-doc-1",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
        acl_ref=_CONFIDENTIAL,
    )
    await store.add_fact(fact)
    return store


def _assert_no_forbidden_leak(result_warnings: list[str]) -> None:
    """Every string that reveals the forbidden entity's existence must be absent."""
    for forbidden in (
        _FORBIDDEN_SUBJECT,
        _FORBIDDEN_OBJECT,
        _FORBIDDEN_SUBJECT.lower(),
        _FORBIDDEN_OBJECT.lower(),
    ):
        for w in result_warnings:
            assert forbidden not in w, (
                f"forbidden entity {forbidden!r} leaked through warnings: {w!r}"
            )


async def test_exact_match_seed_hidden_entity_does_not_leak(
    confidential_only_store: NetworkxStore, ontology: Ontology
) -> None:
    """Caller asks for the forbidden entity by exact id — must look like 'unknown'."""
    plan = Plan.build(
        seed=SeedByEntity(entity_id=_FORBIDDEN_SUBJECT),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(
        plan,
        confidential_only_store,
        allowed_acls=["acl:public"],  # does NOT include acl:confidential
    )

    assert result.hits == []
    # The "unknown entity" warning must fire — this must be
    # indistinguishable from the entity simply not existing.
    assert any("plan.seed produced no entities" in w for w in result.warnings)
    # And no resolution warning may echo the forbidden entity.
    _assert_no_forbidden_leak(result.warnings)


async def test_type_suffix_strip_hidden_entity_does_not_leak(
    confidential_only_store: NetworkxStore, ontology: Ontology
) -> None:
    """Suffix-strip path must honour ACL — forbidden entity stays hidden."""
    plan = Plan.build(
        seed=SeedByEntity(entity_id=f"{_FORBIDDEN_SUBJECT} (Project)"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(
        plan,
        confidential_only_store,
        allowed_acls=["acl:public"],
    )

    assert result.hits == []
    assert any("plan.seed produced no entities" in w for w in result.warnings)
    _assert_no_forbidden_leak(result.warnings)


async def test_keyword_fallback_hidden_entity_does_not_leak(
    confidential_only_store: NetworkxStore, ontology: Ontology
) -> None:
    """Keyword-search fallback must honour ACL — forbidden entity stays hidden."""
    plan = Plan.build(
        seed=SeedByEntity(entity_id="classified project"),  # forces search fallback
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(
        plan,
        confidential_only_store,
        allowed_acls=["acl:public"],
    )

    assert result.hits == []
    assert any("plan.seed produced no entities" in w for w in result.warnings)
    _assert_no_forbidden_leak(result.warnings)


async def test_authorised_caller_still_sees_the_entity(
    confidential_only_store: NetworkxStore, ontology: Ontology
) -> None:
    """Positive control — the caller WITH the right ACL still gets the fact.

    Guards against an over-tight ACL filter that'd break legitimate access.
    """
    plan = Plan.build(
        seed=SeedByEntity(entity_id=_FORBIDDEN_SUBJECT),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(
        plan,
        confidential_only_store,
        allowed_acls=[_CONFIDENTIAL],
    )

    assert result.hits
    assert any(h.fact.subject_id == _FORBIDDEN_SUBJECT for h in result.hits)
