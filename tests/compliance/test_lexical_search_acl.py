"""#119 compliance: forbidden facts never influence lexical search.

BM25 depends on corpus statistics. If forbidden facts counted toward them,
a caller could infer their existence from how visible results are ordered.
Results and ranks must be identical whether or not forbidden data exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.lexical import rank_bm25
from ontos.storage.networkx_store import NetworkxStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
QUERY = "acme widget"


def _provenance() -> Provenance:
    return Provenance(
        source_id="doc.md",
        extractor_id="test",
        extractor_version="1",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


def _fact(subject: str, predicate: str, obj: str, *, n: int, acl_ref: str | None = None) -> Fact:
    return Fact(
        id=UUID(int=n),
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=_provenance(),
        t_valid=T0,
        ingested_at=T0 + timedelta(seconds=n),
        acl_ref=acl_ref,
    )


# Visible: one fact mentions "acme", one "widget", same length. With
# visible-only statistics they tie, so ingest order decides (widget first).
VISIBLE = [
    _fact("x:widget", "rel", "x:one", n=1),
    _fact("x:acme", "rel", "x:two", n=2),
]
# Forbidden: many "widget" facts. Counted globally, they would lower
# idf(widget) and flip the visible order.
FORBIDDEN = [_fact("x:widget", "rel", f"x:hr{n}", n=100 + n, acl_ref="acl:hr") for n in range(8)]


async def _store(*, with_forbidden: bool) -> NetworkxStore:
    store = NetworkxStore()
    for fact in VISIBLE + (FORBIDDEN if with_forbidden else []):
        await store.add_fact(fact)
    return store


def test_corpus_would_leak_under_global_statistics() -> None:
    """Guard: the fixture must actually distinguish global from visible stats."""

    def order(facts: list[Fact]) -> list[UUID]:
        ranked = rank_bm25(QUERY, [(f.id, [f.subject_id, f.predicate, f.object_id]) for f in facts])
        ranked.sort(key=lambda pair: (-pair[0], pair[1]))
        return [key for _, key in ranked if key in {v.id for v in VISIBLE}]

    assert order(VISIBLE) != order(VISIBLE + FORBIDDEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        {"acl_subject": None, "allowed_acls": None},
        {"acl_subject": "user:intern", "allowed_acls": []},
        {"acl_subject": "user:finance", "allowed_acls": ["acl:finance"]},
    ],
)
async def test_results_and_ranks_identical_with_or_without_forbidden_facts(
    caller: dict[str, object],
) -> None:
    without = await (await _store(with_forbidden=False)).search(QUERY, mode="lexical", **caller)  # type: ignore[arg-type]
    with_ = await (await _store(with_forbidden=True)).search(QUERY, mode="lexical", **caller)  # type: ignore[arg-type]

    assert [f.id for f in with_] == [f.id for f in without] == [UUID(int=1), UUID(int=2)]


@pytest.mark.asyncio
async def test_authorized_caller_does_see_the_restricted_facts() -> None:
    store = await _store(with_forbidden=True)

    hits = await store.search(QUERY, mode="lexical", k=20, allowed_acls=["acl:hr"])

    assert {f.id for f in FORBIDDEN} <= {f.id for f in hits}


@pytest.mark.asyncio
async def test_restricted_entity_names_and_aliases_never_match() -> None:
    """A public fact pointing at an entity a restricted document claimed."""
    store = NetworkxStore()
    await store.add_fact(_fact("Project:p1", "owned_by", "Company:acme", n=1))
    await store.upsert_entity(
        Entity(
            id="Project:p1",
            type="Project",
            canonical_name="Falcon",
            aliases=["Skyhawk"],
            provenance=_provenance(),
        ),
        acl_ref="acl:hr",
    )

    for query in ("falcon", "skyhawk", "falcon acme"):
        assert await store.search(query, mode="lexical", allowed_acls=[]) == []
    assert [
        f.id for f in await store.search("skyhawk", mode="lexical", allowed_acls=["acl:hr"])
    ] == [UUID(int=1)]


@pytest.mark.asyncio
async def test_closed_facts_do_not_affect_current_ranks() -> None:
    store = await _store(with_forbidden=False)
    baseline = [f.id for f in await store.search(QUERY, mode="lexical")]

    for n in range(8):
        closed = _fact("x:widget", "rel", f"x:old{n}", n=200 + n)
        await store.add_fact(closed)
        await store.close_fact(closed.id, T0 + timedelta(days=1))

    assert [f.id for f in await store.search(QUERY, mode="lexical")] == baseline
