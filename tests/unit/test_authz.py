"""M2.a — authz backend + store-level allowed_acls filtering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontos.authz import VIEW, AuthzBackend, InMemoryAuthz
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _make_fact(subject: str, predicate: str, obj: str, *, acl_ref: str | None = None) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="t",
            extractor_id="t",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=now,
        ingested_at=now,
        acl_ref=acl_ref,
    )


def test_inmemory_authz_satisfies_protocol() -> None:
    assert isinstance(InMemoryAuthz(), AuthzBackend)


async def test_inmemory_grant_and_check() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:hr")
    assert await authz.check("user:alice", VIEW, "acl:hr")
    assert not await authz.check("user:alice", VIEW, "acl:finance")
    assert not await authz.check("user:bob", VIEW, "acl:hr")


async def test_inmemory_list_authorized_objects_is_sorted() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:c")
    authz.grant("user:alice", VIEW, "acl:a")
    authz.grant("user:alice", VIEW, "acl:b")
    assert await authz.list_authorized_objects("user:alice", VIEW) == [
        "acl:a", "acl:b", "acl:c"
    ]


async def test_revoke_removes_grant() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:hr")
    authz.revoke("user:alice", VIEW, "acl:hr")
    assert not await authz.check("user:alice", VIEW, "acl:hr")


async def test_list_returns_empty_for_unknown_subject() -> None:
    authz = InMemoryAuthz()
    assert await authz.list_authorized_objects("user:ghost", VIEW) == []


async def test_store_allowed_acls_filters_facts() -> None:
    store = NetworkxStore()
    public = _make_fact("A", "knows", "B")
    hr = _make_fact("A", "reports_to", "C", acl_ref="acl:hr")
    finance = _make_fact("A", "manages_budget_for", "D", acl_ref="acl:finance")
    await store.add_fact(public)
    await store.add_fact(hr)
    await store.add_fact(finance)

    # Caller with just acl:hr sees public + hr but not finance.
    hits = await store.search("A", allowed_acls=["acl:hr"])
    hit_ids = {f.id for f in hits}
    assert public.id in hit_ids
    assert hr.id in hit_ids
    assert finance.id not in hit_ids


async def test_store_empty_allowed_acls_still_shows_public_facts() -> None:
    store = NetworkxStore()
    public = _make_fact("A", "knows", "B")
    private = _make_fact("A", "reports_to", "C", acl_ref="acl:hr")
    await store.add_fact(public)
    await store.add_fact(private)

    hits = await store.search("A", allowed_acls=[])
    assert len(hits) == 1
    assert hits[0].id == public.id


async def test_store_denies_by_default_when_acl_ref_set_and_no_subject() -> None:
    store = NetworkxStore()
    private = _make_fact("A", "reports_to", "C", acl_ref="acl:hr")
    await store.add_fact(private)

    # No acl_subject, no allowed_acls, but the fact has an acl_ref. Deny.
    assert await store.search("reports_to") == []


async def test_allowed_acls_takes_precedence_over_acl_subject() -> None:
    store = NetworkxStore()
    tagged = _make_fact("A", "reports_to", "C", acl_ref="acl:hr")
    await store.add_fact(tagged)

    # M1 shim path would have matched via acl_subject="acl:hr"; but if
    # allowed_acls says otherwise, allowed_acls wins.
    hits = await store.search(
        "reports_to", acl_subject="acl:hr", allowed_acls=[]
    )
    assert hits == []


async def test_traverse_uses_allowed_acls_and_prunes_paths() -> None:
    store = NetworkxStore()
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("B", "knows", "C", acl_ref="acl:hr"))
    await store.add_fact(_make_fact("C", "knows", "D", acl_ref="acl:hr"))

    # Caller without acl:hr — traversal stops at B, C never expanded.
    result = list(await store.traverse("A", depth=5, allowed_acls=[]))
    assert len(result) == 1
    assert result[0].object_id == "B"


async def test_facts_for_entity_honors_allowed_acls() -> None:
    store = NetworkxStore()
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("A", "reports_to", "C", acl_ref="acl:hr"))

    with_hr = await store.facts_for_entity("A", allowed_acls=["acl:hr"])
    without_hr = await store.facts_for_entity("A", allowed_acls=[])

    assert len(with_hr) == 2
    assert len(without_hr) == 1


async def test_authz_error_is_a_runtime_error() -> None:
    from ontos.authz import AuthzError

    assert issubclass(AuthzError, RuntimeError)


@pytest.mark.parametrize(
    "acl_ref,acl_subject,allowed_acls,expected",
    [
        # Public facts are always visible.
        (None, None, None, True),
        (None, "anyone", ["anything"], True),
        # M2 authz-resolved path.
        ("acl:hr", None, ["acl:hr"], True),
        ("acl:hr", None, ["acl:finance"], False),
        ("acl:hr", None, [], False),
        # M1 shim (used only when allowed_acls is None).
        ("acl:hr", "acl:hr", None, True),
        ("acl:hr", "acl:finance", None, False),
        # Deny-by-default when acl_ref is set and neither is provided.
        ("acl:hr", None, None, False),
        # allowed_acls wins over acl_subject when both are provided.
        ("acl:hr", "acl:hr", [], False),
    ],
)
async def test_acl_allows_matrix(
    acl_ref: str | None,
    acl_subject: str | None,
    allowed_acls: list[str] | None,
    expected: bool,
) -> None:
    from ontos.storage.networkx_store import _acl_allows

    fact = _make_fact("s", "p", "o", acl_ref=acl_ref)
    allowed_set = set(allowed_acls) if allowed_acls is not None else None
    assert _acl_allows(fact, acl_subject, allowed_set) is expected
