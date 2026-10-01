"""Gate-blocking compliance tests for issue #21 supersession.

The reviewer on PR #26 (comment 4153410732) correctly flagged that
this PR changes ACL-scoped write behaviour and bitemporal semantics
without landing anything under `tests/compliance/`, which is the
gate-blocking suite. The tests here pin:

- same-ACL supersession fires (positive compliance baseline)
- cross-ACL isolation (public ⊥ confidential; A ⊥ B)
- cross-tier scope: a new confidential fact leaves an active public
  fact alive, and vice versa — same (s, p) but different ACL means
  "two independent namespaces," not "contradiction"
- bitemporal `as_of` returns the correct historical / current fact
- out-of-order `t_valid` does NOT close the newer active fact

A regression on any of these breaks a non-negotiable invariant of
the Ontos pitch (EU AI Act Article 12 + the ACL-as-namespace
contract) and the gate must fail.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ontos.extraction import LlmExtractor, RawTriple
from ontos.ingest import TextConnector
from ontos.ontology import Ontology, load_ontology
from ontos.pipeline import IngestPipeline
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore
from ontos.supersession import CardinalitySupersessionPolicy

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


class _ScriptedLLM:
    id: str = "compliance-scripted-llm"
    version: str = "0"

    def __init__(self, script: dict[str, list[RawTriple]]) -> None:
        self._script = script

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        return list(self._script.get(text, []))


def _triple(
    subject: str,
    s_type: str,
    s_name: str,
    predicate: str,
    obj: str,
    o_type: str,
    o_name: str,
) -> RawTriple:
    return RawTriple(
        subject_id=subject,
        subject_type=s_type,
        subject_canonical_name=s_name,
        predicate=predicate,
        object_id=obj,
        object_type=o_type,
        object_canonical_name=o_name,
        llm_confidence=0.95,
    )


# ────────────────────────────────────────────────────────────────────
# Baseline: supersession fires inside a single ACL
# ────────────────────────────────────────────────────────────────────


async def test_compliance_same_acl_supersession_fires(ontology: Ontology) -> None:
    store = NetworkxStore()
    old = _mk_fact("David", "works_at", "Beta", acl_ref="confidential")
    new = _mk_fact("David", "works_at", "Acme", acl_ref="confidential")
    await store.add_fact(old)

    decision = await CardinalitySupersessionPolicy().decide(new, store, ontology)
    assert [f.id for f in decision.facts_to_close] == [old.id], (
        "same-ACL correction MUST supersede the old fact"
    )


# ────────────────────────────────────────────────────────────────────
# Cross-ACL isolation — the ACL-as-namespace invariant
# ────────────────────────────────────────────────────────────────────


async def test_compliance_public_ingest_cannot_close_confidential_prior(
    ontology: Ontology,
) -> None:
    """A public ingest must NEVER close a confidential prior fact."""
    store = NetworkxStore()
    confidential = _mk_fact("David", "works_at", "Beta", acl_ref="confidential")
    await store.add_fact(confidential)
    public_new = _mk_fact("David", "works_at", "Acme", acl_ref=None)

    decision = await CardinalitySupersessionPolicy().decide(public_new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.new_fact_t_invalid is None


async def test_compliance_confidential_a_cannot_close_confidential_b(
    ontology: Ontology,
) -> None:
    store = NetworkxStore()
    confidential_a = _mk_fact("David", "works_at", "Beta", acl_ref="confidential-A")
    await store.add_fact(confidential_a)
    confidential_b_new = _mk_fact("David", "works_at", "Acme", acl_ref="confidential-B")

    decision = await CardinalitySupersessionPolicy().decide(confidential_b_new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.new_fact_t_invalid is None


async def test_compliance_confidential_new_leaves_public_active(
    ontology: Ontology,
) -> None:
    """A new confidential fact for same (s, p) must NOT disturb an
    existing active public fact.

    Reviewer's explicit ask on PR #26: "a new *confidential* fact
    vs. an active *public* fact for the same `(s,p)` (currently both
    stay active; confirm that's intended and pin it with a test)."
    Intended = yes; public and confidential are independent namespaces.
    """
    store = NetworkxStore()
    public_old = _mk_fact("David", "works_at", "Beta", acl_ref=None)
    await store.add_fact(public_old)
    confidential_new = _mk_fact("David", "works_at", "Acme", acl_ref="confidential")

    decision = await CardinalitySupersessionPolicy().decide(confidential_new, store, ontology)
    assert decision.facts_to_close == []
    assert decision.new_fact_t_invalid is None


# ────────────────────────────────────────────────────────────────────
# Bitemporal correctness
# ────────────────────────────────────────────────────────────────────


async def test_compliance_as_of_before_correction_returns_old_fact(
    ontology: Ontology,
) -> None:
    """`as_of` BEFORE the correction returns the old (now-closed) fact.
    This is the Article-12 "what did the system see at time T?"
    answer. If this breaks, the compliance pitch is dead.
    """
    script = {
        "David Lee works at Beta Systems.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:beta",
                "Company",
                "Beta Systems",
            )
        ],
        "David Lee works at Acme Corp.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:acme",
                "Company",
                "Acme Corp",
            )
        ],
    }
    store = NetworkxStore()
    extractor = LlmExtractor(_ScriptedLLM(script), ontology)
    await IngestPipeline(
        connector=TextConnector({"test.txt": "David Lee works at Beta Systems."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    before_correction = datetime.now(UTC)
    await asyncio.sleep(0.02)
    await IngestPipeline(
        connector=TextConnector({"update.txt": "David Lee works at Acme Corp."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    after_correction = datetime.now(UTC)

    historical = await store.facts_for_entity("person:david-lee", as_of=before_correction)
    historical_works_at = [f for f in historical if f.predicate == "works_at"]
    assert len(historical_works_at) == 1
    assert historical_works_at[0].object_id == "company:beta"

    current = await store.facts_for_entity("person:david-lee", as_of=after_correction)
    current_works_at = [f for f in current if f.predicate == "works_at"]
    assert len(current_works_at) == 1
    assert current_works_at[0].object_id == "company:acme"


# ────────────────────────────────────────────────────────────────────
# Out-of-order t_valid — reviewer's blocking concern pinned
# ────────────────────────────────────────────────────────────────────


async def test_compliance_backfill_does_not_close_newer_active(
    ontology: Ontology,
) -> None:
    """A backfilled older claim MUST NOT close the current active fact.

    Pre-fix (reviewer's reproduced sandbox): ingesting an older claim
    after a newer one closed the newer, correct fact and left the
    stale claim active. The new policy partitions candidates by
    t_valid and writes the historical fact pre-closed instead.
    """
    store = NetworkxStore()
    now = datetime.now(UTC)
    current = _mk_fact("David", "works_at", "Acme", t_valid=now)
    await store.add_fact(current)

    backfilled = _mk_fact("David", "works_at", "Beta", t_valid=now - timedelta(days=365))
    decision = await CardinalitySupersessionPolicy().decide(backfilled, store, ontology)

    assert decision.facts_to_close == [], (
        "backfill must not close the newer active fact; bitemporal correctness invariant"
    )
    assert decision.new_fact_t_invalid == current.t_valid
    assert decision.new_fact_superseded_by == current.id
