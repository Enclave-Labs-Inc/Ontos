"""Unit tests for CardinalitySupersessionPolicy (GitHub issue #21).

Each test is isolated to the policy — no pipeline, no extractor.
`NetworkxStore` is used as the backend because `CardinalitySupersession
Policy` only touches `facts_for_entity`, which has identical semantics
on NetworkxStore and Neo4jStore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.ontology import Ontology, load_ontology
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore
from ontos.supersession import (
    CardinalitySupersessionPolicy,
    NullSupersessionPolicy,
    SupersessionPolicy,
)

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


def _mk_fact(
    subject: str,
    predicate: str,
    obj: str,
    *,
    acl_ref: str | None = None,
    source_id: str = "test",
    t_valid: datetime | None = None,
) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id=source_id,
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=t_valid if t_valid is not None else now,
        ingested_at=now,
        acl_ref=acl_ref,
    )


# ────────────────────────────────────────────────────────────────────
# Protocol conformance
# ────────────────────────────────────────────────────────────────────


def test_cardinality_policy_conforms_to_protocol() -> None:
    assert isinstance(CardinalitySupersessionPolicy(), SupersessionPolicy)


def test_null_policy_conforms_to_protocol() -> None:
    assert isinstance(NullSupersessionPolicy(), SupersessionPolicy)


# ────────────────────────────────────────────────────────────────────
# Cardinality-based decisions
# ────────────────────────────────────────────────────────────────────


async def test_many_to_one_relation_closes_older_fact(ontology: Ontology) -> None:
    """`works_at` is many_to_one — David can only work at one company."""
    store = NetworkxStore()
    old = _mk_fact("David", "works_at", "Beta Systems")
    new = _mk_fact("David", "works_at", "Acme Corp")
    await store.add_fact(old)

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)

    assert decision.skip_add is False
    assert [f.id for f in decision.facts_to_close] == [old.id]
    assert decision.record is not None
    assert decision.record.new_fact_id == new.id
    assert decision.record.closed_fact_ids == [old.id]
    assert decision.record.policy_id == "ontos.supersession.cardinality"


async def test_many_to_many_relation_does_nothing(ontology: Ontology) -> None:
    """`acquired` is many_to_many — Acme can acquire multiple companies."""
    store = NetworkxStore()
    old = _mk_fact("Acme Corp", "acquired", "Beta Systems")
    new = _mk_fact("Acme Corp", "acquired", "Gamma Co")
    await store.add_fact(old)

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)

    assert decision.facts_to_close == []
    assert decision.skip_add is False
    assert decision.record is None


async def test_one_to_one_relation_closes_older_fact(tmp_path: Path) -> None:
    """A one_to_one relation triggers supersession on new fact for same subject."""
    # Craft a tiny ontology with a one_to_one relation.
    ont_file = tmp_path / "ont.yaml"
    ont_file.write_text(
        "entity_types:\n"
        "  - label: Person\n"
        "relation_types:\n"
        "  - label: spouse_of\n"
        "    cardinality: one_to_one\n"
        "patterns:\n"
        "  - subject_type: Person\n"
        "    predicate: spouse_of\n"
        "    object_type: Person\n"
    )
    ontology = load_ontology(ont_file)
    store = NetworkxStore()
    old = _mk_fact("Alice", "spouse_of", "Bob")
    new = _mk_fact("Alice", "spouse_of", "Carol")
    await store.add_fact(old)

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert [f.id for f in decision.facts_to_close] == [old.id]


async def test_unknown_relation_returns_empty_decision(ontology: Ontology) -> None:
    """A predicate the ontology doesn't know → no supersession."""
    store = NetworkxStore()
    new = _mk_fact("A", "unknown_rel", "B")
    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.skip_add is False


# ────────────────────────────────────────────────────────────────────
# Re-affirmation special case
# ────────────────────────────────────────────────────────────────────


async def test_same_source_re_affirmation_sets_skip_add(ontology: Ontology) -> None:
    """Identical (s, p, o, source_id) → skip_add + audit record."""
    store = NetworkxStore()
    first = _mk_fact("David", "works_at", "Beta Systems", source_id="hr.txt")
    await store.add_fact(first)
    duplicate = _mk_fact("David", "works_at", "Beta Systems", source_id="hr.txt")

    decision = await CardinalitySupersessionPolicy().decide(duplicate, store, ontology)
    assert decision.skip_add is True
    assert decision.facts_to_close == []
    # Audit visibility: idempotent re-ingest MUST still produce a record.
    assert decision.record is not None
    assert "idempotent re-ingest" in decision.record.reason


async def test_different_source_corroboration_writes_new_fact(ontology: Ontology) -> None:
    """A different source asserting the same (s, p, o) → write BOTH facts.

    CLAUDE.md: provenance may never be dropped. If `hr.txt` says
    `David works_at Beta` and later `payroll.txt` independently
    says the same thing, both source attestations must survive.
    """
    store = NetworkxStore()
    first = _mk_fact("David", "works_at", "Beta Systems", source_id="hr.txt")
    await store.add_fact(first)
    corroboration = _mk_fact("David", "works_at", "Beta Systems", source_id="payroll.txt")

    decision = await CardinalitySupersessionPolicy().decide(corroboration, store, ontology)
    assert decision.skip_add is False, "second source must not be silently dropped"
    assert decision.facts_to_close == [], (
        "corroborating fact must not close the original; both represent provenance"
    )
    assert decision.new_fact_t_invalid is None
    assert decision.record is None, (
        "no supersession fires on same-value corroboration; policy stays quiet"
    )


# ────────────────────────────────────────────────────────────────────
# t_valid ordering — out-of-order / backfill ingest
# ────────────────────────────────────────────────────────────────────


async def test_newer_fact_closes_older_active(ontology: Ontology) -> None:
    """Standard supersession path: new.t_valid > existing.t_valid."""
    from datetime import timedelta

    store = NetworkxStore()
    now = datetime.now(UTC)
    old = _mk_fact("David", "works_at", "Beta Systems", t_valid=now - timedelta(days=30))
    await store.add_fact(old)
    new = _mk_fact("David", "works_at", "Acme Corp", t_valid=now)

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert [f.id for f in decision.facts_to_close] == [old.id]
    assert decision.new_fact_t_invalid is None


async def test_older_fact_does_not_close_newer_active(ontology: Ontology) -> None:
    """Backfill: new.t_valid < existing.t_valid → do NOT close the newer.

    Reviewer's blocking concern: previously the policy would close
    the correct current fact (Acme) because an older (Beta, t_valid
    year ago) ingest came through. Now the historical fact is
    written PRE-CLOSED and the active current fact is untouched.
    """
    from datetime import timedelta

    store = NetworkxStore()
    now = datetime.now(UTC)
    current = _mk_fact("David", "works_at", "Acme Corp", t_valid=now)
    await store.add_fact(current)
    backfill = _mk_fact("David", "works_at", "Beta Systems", t_valid=now - timedelta(days=365))

    decision = await CardinalitySupersessionPolicy().decide(backfill, store, ontology)

    assert decision.facts_to_close == [], (
        "backfill of an older claim must not close the newer active fact"
    )
    assert decision.new_fact_t_invalid == current.t_valid, (
        "backfilled fact should be written pre-closed at the moment the current fact took effect"
    )
    assert decision.new_fact_superseded_by == current.id
    assert decision.record is not None
    assert "historical ingest" in decision.record.reason


# ────────────────────────────────────────────────────────────────────
# Empty / multi-stale cases
# ────────────────────────────────────────────────────────────────────


async def test_no_existing_active_fact_returns_empty_decision(
    ontology: Ontology,
) -> None:
    store = NetworkxStore()
    new = _mk_fact("David", "works_at", "Acme Corp")
    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.skip_add is False


async def test_closes_every_pre_existing_stale_fact(ontology: Ontology) -> None:
    """A store with multiple stale facts for the same (s, p) gets repaired.

    Pre-#21 stores could already hold two active `David Lee works_at`
    facts. On the next matching ingestion, the policy closes ALL of
    them — data-repair semantics without needing a dedicated CLI.
    """
    store = NetworkxStore()
    stale_1 = _mk_fact("David", "works_at", "Beta Systems")
    stale_2 = _mk_fact("David", "works_at", "Gamma Industries")
    await store.add_fact(stale_1)
    await store.add_fact(stale_2)

    new = _mk_fact("David", "works_at", "Acme Corp")
    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)

    closed_ids = {f.id for f in decision.facts_to_close}
    assert closed_ids == {stale_1.id, stale_2.id}


async def test_already_closed_facts_are_ignored(ontology: Ontology) -> None:
    """`close_fact` sets t_invalid; those must not re-close."""
    store = NetworkxStore()
    old = _mk_fact("David", "works_at", "Beta Systems")
    await store.add_fact(old)
    await store.close_fact(old.id, t_invalid=datetime.now(UTC), superseded_by=None)

    new = _mk_fact("David", "works_at", "Acme Corp")
    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert decision.facts_to_close == []


# ────────────────────────────────────────────────────────────────────
# ACL scope — same-ACL only
# ────────────────────────────────────────────────────────────────────


async def test_public_ingest_does_not_touch_confidential_prior_fact(
    ontology: Ontology,
) -> None:
    """New public fact must leave a prior confidential fact alone."""
    store = NetworkxStore()
    confidential = _mk_fact("David", "works_at", "Beta Systems", acl_ref="confidential")
    await store.add_fact(confidential)
    public_new = _mk_fact("David", "works_at", "Acme Corp")

    decision = await CardinalitySupersessionPolicy().decide(public_new, store, ontology)
    assert decision.facts_to_close == []


async def test_cross_acl_facts_do_not_supersede_each_other(
    ontology: Ontology,
) -> None:
    """confidential-A must not touch confidential-B facts."""
    store = NetworkxStore()
    confidential_a = _mk_fact("David", "works_at", "Beta Systems", acl_ref="confidential-A")
    await store.add_fact(confidential_a)
    confidential_b_new = _mk_fact("David", "works_at", "Acme Corp", acl_ref="confidential-B")

    decision = await CardinalitySupersessionPolicy().decide(confidential_b_new, store, ontology)
    assert decision.facts_to_close == []


async def test_same_acl_supersession_fires(ontology: Ontology) -> None:
    """New confidential-A fact DOES close older confidential-A fact."""
    store = NetworkxStore()
    old = _mk_fact("David", "works_at", "Beta Systems", acl_ref="confidential-A")
    await store.add_fact(old)
    new = _mk_fact("David", "works_at", "Acme Corp", acl_ref="confidential-A")

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert [f.id for f in decision.facts_to_close] == [old.id]


# ────────────────────────────────────────────────────────────────────
# NullSupersessionPolicy
# ────────────────────────────────────────────────────────────────────


async def test_null_policy_never_closes_anything(ontology: Ontology) -> None:
    store = NetworkxStore()
    old = _mk_fact("David", "works_at", "Beta Systems")
    new = _mk_fact("David", "works_at", "Acme Corp")
    await store.add_fact(old)

    decision = await NullSupersessionPolicy().decide(new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.skip_add is False
    assert decision.record is None
