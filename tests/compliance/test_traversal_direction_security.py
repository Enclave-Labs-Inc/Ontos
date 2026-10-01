"""Compliance regressions for direction-aware graph traversal.

Issue #17 adds incoming and bidirectional traversal paths. These tests
pin the security invariants that ACL and bitemporal filters must apply
during those new traversal paths, not as post-processing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

_CONFIDENTIAL = "acl:confidential"


@pytest.fixture
async def store() -> NetworkxStore:
    return NetworkxStore()


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
            source_id="compliance-test",
            extractor_id="test",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=t_valid or now,
        ingested_at=now,
        acl_ref=acl_ref,
    )


async def test_incoming_traversal_does_not_cross_forbidden_acl(
    store: NetworkxStore,
) -> None:
    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme",
        acl_ref=_CONFIDENTIAL,
    )
    await store.add_fact(fact)

    results = list(
        await store.traverse(
            "company:acme",
            relation="works_at",
            direction="in",
            depth=1,
            allowed_acls=["acl:public"],
        )
    )

    assert results == []


async def test_bidirectional_traversal_does_not_expand_through_forbidden_edge(
    store: NetworkxStore,
) -> None:
    secret_edge = _make_fact(
        "person:alice",
        "works_at",
        "company:acme",
        acl_ref=_CONFIDENTIAL,
    )
    public_edge_beyond_secret = _make_fact(
        "person:alice",
        "knows",
        "person:bob",
    )

    await store.add_fact(secret_edge)
    await store.add_fact(public_edge_beyond_secret)

    results = list(
        await store.traverse(
            "company:acme",
            direction="both",
            depth=2,
            allowed_acls=["acl:public"],
        )
    )

    assert results == []


async def test_incoming_traversal_respects_as_of(
    store: NetworkxStore,
) -> None:
    t_valid = datetime.now(UTC) - timedelta(days=30)
    t_invalid = datetime.now(UTC) - timedelta(days=10)

    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme",
        t_valid=t_valid,
    )
    await store.add_fact(fact)
    await store.close_fact(fact.id, t_invalid=t_invalid)

    current = list(
        await store.traverse(
            "company:acme",
            relation="works_at",
            direction="in",
            depth=1,
        )
    )

    historical = list(
        await store.traverse(
            "company:acme",
            relation="works_at",
            direction="in",
            depth=1,
            as_of=datetime.now(UTC) - timedelta(days=20),
        )
    )

    assert current == []
    assert len(historical) == 1
    assert historical[0].id == fact.id
