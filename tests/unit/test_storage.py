"""NetworkX store: add / close / search / traverse / bitemporal / acl."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _make_fact(
    subject: str,
    predicate: str,
    obj: str,
    *,
    t_valid: datetime | None = None,
    acl_ref: str | None = None,
) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="test",
            extractor_id="test-extractor",
            extractor_version="0.0.1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=t_valid or now,
        ingested_at=now,
        acl_ref=acl_ref,
    )


async def test_add_and_get(store: NetworkxStore) -> None:
    fact = _make_fact("A", "knows", "B")
    await store.add_fact(fact)
    got = await store.get_fact(fact.id)
    assert got == fact


async def test_search_substring_match(store: NetworkxStore) -> None:
    fact = _make_fact("Acme Corp", "acquired", "Widget Inc")
    await store.add_fact(fact)
    hits = await store.search("acquired", k=5)
    assert len(hits) == 1
    assert hits[0].id == fact.id


async def test_search_excludes_closed_facts(store: NetworkxStore) -> None:
    fact = _make_fact("A", "reports_to", "B")
    await store.add_fact(fact)
    await store.close_fact(fact.id, t_invalid=datetime.now(UTC), superseded_by=uuid4())
    # Default `as_of=None` means "only currently-valid facts."
    assert await store.search("reports_to") == []


async def test_as_of_returns_historical_facts(store: NetworkxStore) -> None:
    past = datetime.now(UTC) - timedelta(days=30)
    fact = _make_fact("A", "reports_to", "B", t_valid=past)
    await store.add_fact(fact)
    later = datetime.now(UTC)
    await store.close_fact(fact.id, t_invalid=later)
    # As of 15 days ago, the fact was still alive.
    when = datetime.now(UTC) - timedelta(days=15)
    hits = await store.search("reports_to", as_of=when)
    assert len(hits) == 1


async def test_traverse_respects_depth(store: NetworkxStore) -> None:
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("B", "knows", "C"))
    await store.add_fact(_make_fact("C", "knows", "D"))
    depth1 = list(await store.traverse("A", depth=1))
    depth2 = list(await store.traverse("A", depth=2))
    assert len(depth1) == 1
    assert len(depth2) == 2


async def test_traverse_incoming_direction(store: NetworkxStore) -> None:
    fact = _make_fact("person:alice", "works_at", "company:acme")
    await store.add_fact(fact)

    results = list(
        await store.traverse(
            "company:acme",
            relation="works_at",
            direction="in",
            depth=1,
        )
    )

    assert len(results) == 1
    assert results[0].subject_id == "person:alice"
    assert results[0].predicate == "works_at"
    assert results[0].object_id == "company:acme"


async def test_acl_blocks_forbidden_facts_from_search(store: NetworkxStore) -> None:
    public = _make_fact("A", "knows", "B")
    private = _make_fact("A", "knows", "C", acl_ref="alice")
    await store.add_fact(public)
    await store.add_fact(private)
    # Alice sees both.
    assert len(await store.search("knows", acl_subject="alice")) == 2
    # Bob sees only public.
    hits = await store.search("knows", acl_subject="bob")
    assert len(hits) == 1
    assert hits[0].id == public.id


async def test_acl_prunes_forbidden_paths_during_traversal(store: NetworkxStore) -> None:
    # Path A -> B (public) -> C (alice-only). Bob traversing from A must not
    # reach C, even indirectly. The forbidden edge is skipped during traversal
    # so C is never expanded.
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("B", "knows", "C", acl_ref="alice"))
    bob_view = list(await store.traverse("A", depth=3, acl_subject="bob"))
    assert all(fact.object_id != "C" for fact in bob_view)


@pytest.mark.parametrize("depth", [0, 1, 5])
async def test_traverse_returns_empty_for_unknown_start(store: NetworkxStore, depth: int) -> None:
    assert list(await store.traverse("nonexistent", depth=depth)) == []
