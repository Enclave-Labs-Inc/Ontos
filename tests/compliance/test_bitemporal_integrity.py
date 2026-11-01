"""Gate-blocking: store writes never rewrite history (#95).

- `close_fact` on an already-closed fact silently moved its `t_invalid`
  and replaced `superseded_by`.
- A duplicate `add_fact` replaced the fact in NetworkX and created a
  second edge with the same `fact_id` in Neo4j.
- Ingest added the new fact and closed the old one in separate calls, so
  a failed close left two active facts in one scope.

The `check_*` bodies are shared with the Neo4j integration tests so both
backends are held to the same contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ontos.storage.base import FactAlreadyExistsError, FactNotOpenError, GraphStore
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance._mcp_harness import make_fact

_T1 = datetime(2026, 1, 1, tzinfo=UTC)
_T2 = _T1 + timedelta(days=30)


async def check_double_close_keeps_the_original_window(store: GraphStore) -> None:
    fact = make_fact("Acme", "acl:finance")
    first_successor, second_successor = uuid4(), uuid4()
    await store.add_fact(fact)
    await store.close_fact(fact.id, t_invalid=_T1, superseded_by=first_successor)

    with pytest.raises(FactNotOpenError):
        await store.close_fact(fact.id, t_invalid=_T2, superseded_by=second_successor)

    stored = await store.get_fact(fact.id)
    assert stored is not None
    assert stored.t_invalid == _T1
    assert stored.superseded_by == first_successor


async def check_closing_a_missing_fact_is_a_noop(store: GraphStore) -> None:
    await store.close_fact(uuid4(), t_invalid=_T1)


async def check_duplicate_add_is_rejected(store: GraphStore) -> None:
    fact = make_fact("Acme", "acl:finance")
    await store.add_fact(fact)

    with pytest.raises(FactAlreadyExistsError):
        await store.add_fact(fact.model_copy(update={"predicate": "rewritten"}))

    stored = await store.get_fact(fact.id)
    assert stored is not None
    assert stored.predicate == fact.predicate
    assert [f.id for f in await store.facts_for_entity("Acme", allowed_acls=["acl:finance"])] == [
        fact.id
    ]


async def check_supersede_adds_and_closes(store: GraphStore) -> None:
    old = make_fact("Acme", "acl:finance")
    new = make_fact("Acme", "acl:finance")
    await store.add_fact(old)

    await store.supersede(new, [old.id])

    closed = await store.get_fact(old.id)
    assert closed is not None
    assert closed.t_invalid == new.ingested_at
    assert closed.superseded_by == new.id
    added = await store.get_fact(new.id)
    assert added is not None
    assert added.t_invalid is None


async def check_failed_supersede_leaves_the_store_unchanged(store: GraphStore) -> None:
    open_fact = make_fact("Acme", "acl:finance")
    closed_fact = make_fact("Acme", "acl:finance")
    await store.add_fact(open_fact)
    await store.add_fact(closed_fact)
    await store.close_fact(closed_fact.id, t_invalid=_T1)
    new = make_fact("Acme", "acl:finance")

    with pytest.raises(FactNotOpenError):
        await store.supersede(new, [open_fact.id, closed_fact.id])
    with pytest.raises(FactNotOpenError):
        await store.supersede(new, [open_fact.id, uuid4()])

    assert await store.get_fact(new.id) is None
    still_open = await store.get_fact(open_fact.id)
    assert still_open is not None
    assert still_open.t_invalid is None
    still_closed = await store.get_fact(closed_fact.id)
    assert still_closed is not None
    assert still_closed.t_invalid == _T1


async def check_supersede_rejects_an_existing_new_fact(store: GraphStore) -> None:
    old = make_fact("Acme", "acl:finance")
    await store.add_fact(old)

    with pytest.raises(FactAlreadyExistsError):
        await store.supersede(old, [old.id])

    stored = await store.get_fact(old.id)
    assert stored is not None
    assert stored.t_invalid is None


CHECKS = [
    check_double_close_keeps_the_original_window,
    check_closing_a_missing_fact_is_a_noop,
    check_duplicate_add_is_rejected,
    check_supersede_adds_and_closes,
    check_failed_supersede_leaves_the_store_unchanged,
    check_supersede_rejects_an_existing_new_fact,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_networkx_store(check) -> None:  # type: ignore[no-untyped-def]
    await check(NetworkxStore())
