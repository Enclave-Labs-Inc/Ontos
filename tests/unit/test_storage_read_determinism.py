"""Storage reads are deterministic and agree across backends (#102).

- Neo4j `search` applied `LIMIT k` to an unordered match, so the top-k
  set could change between calls.
- NetworkX `traverse(direction="both")` returned an edge once from each
  endpoint, and Neo4j deduplicated, so the backends disagreed.

Both stores now rank search by score, then oldest ingest, then fact id,
and return traversals once per fact in that same ingest order.
The `check_*` bodies are shared with the Neo4j integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.base import GraphStore
from ontos.storage.networkx_store import NetworkxStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fact(subject: str, predicate: str, object_: str, *, day: int) -> Fact:
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=object_,
        provenance=Provenance(
            source_id="doc:determinism",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=_T0,
        ingested_at=_T0 + timedelta(days=day),
    )


async def check_traverse_both_returns_each_fact_once(store: GraphStore) -> None:
    ab = _fact("a", "linked", "b", day=3)
    bc = _fact("b", "linked", "c", day=1)
    ca = _fact("c", "linked", "a", day=2)
    for fact in (ab, bc, ca):
        await store.add_fact(fact)

    facts = list(await store.traverse("a", direction="both", depth=3))

    assert [fact.id for fact in facts] == [bc.id, ca.id, ab.id]


async def check_search_ties_break_by_ingest_order(store: GraphStore) -> None:
    newest = _fact("x1", "acquired", "y1", day=3)
    oldest = _fact("x2", "acquired", "y2", day=1)
    middle = _fact("x3", "acquired", "y3", day=2)
    for fact in (newest, oldest, middle):
        await store.add_fact(fact)

    top_two = await store.search("acquired", k=2)

    assert [fact.id for fact in top_two] == [oldest.id, middle.id]


async def check_search_ranks_exact_field_matches_first(store: GraphStore) -> None:
    partial = _fact("acme-holdings", "owns", "z", day=1)
    exact = _fact("acme", "owns", "z2", day=2)
    await store.add_fact(partial)
    await store.add_fact(exact)

    facts = await store.search("acme", k=10)

    assert [fact.id for fact in facts] == [exact.id, partial.id]


async def check_search_scores_names_of_typed_entities_only(store: GraphStore) -> None:
    named = _fact("org:1", "owns", "z", day=2)
    raw = _fact("widget", "owns", "z2", day=1)
    await store.upsert_entity(
        Entity(id="org:1", type="Company", canonical_name="Widget", provenance=named.provenance)
    )
    await store.add_fact(named)
    await store.add_fact(raw)

    facts = await store.search("widget", k=10)

    # Both match exactly ("widget" is a canonical name and an id); ties
    # break on ingest order. A Neo4j raw node's canonical_name (= its id)
    # must not change the score.
    assert [fact.id for fact in facts] == [raw.id, named.id]


CHECKS = [
    check_traverse_both_returns_each_fact_once,
    check_search_ties_break_by_ingest_order,
    check_search_ranks_exact_field_matches_first,
    check_search_scores_names_of_typed_entities_only,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_networkx_store(check) -> None:  # type: ignore[no-untyped-def]
    await check(NetworkxStore())
