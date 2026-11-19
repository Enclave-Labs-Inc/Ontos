"""#119 phase 1: Neo4j lexical search matches NetworkX and leaks nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.neo4j_store import Neo4jStore
from ontos.storage.networkx_store import NetworkxStore

# Reuse the real Docker fixtures from the existing Neo4j suite.
from tests.integration.test_neo4j_store import (
    neo4j_container as neo4j_container,
)
from tests.integration.test_neo4j_store import (
    store as store,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _provenance() -> Provenance:
    return Provenance(
        source_id="doc.md",
        extractor_id="test",
        extractor_version="1",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


def _fact(
    subject: str,
    predicate: str,
    obj: str,
    *,
    n: int,
    acl_ref: str | None = None,
    t_invalid: datetime | None = None,
) -> Fact:
    return Fact(
        id=UUID(int=n),
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=_provenance(),
        t_valid=T0,
        t_invalid=t_invalid,
        ingested_at=T0 + timedelta(seconds=n),
        acl_ref=acl_ref,
    )


ENTITIES = [
    ("Person:dana lee", "Person", "Dana Lee", [], None),
    ("Person:sam ortiz", "Person", "Sam Ortiz", ["Sammy"], None),
    ("Company:acme corp", "Company", "Acme Corp", ["Acme", "ACME Holdings"], None),
    ("Company:globex", "Company", "Globex", [], None),
    ("Company:straße gmbh", "Company", "Straße GmbH", [], None),
    ("Project:falcon@hr", "Project", "Falcon", ["Skyhawk"], "acl:hr"),
]
FACTS = [
    _fact(
        "Person:dana lee", "works_at", "Company:acme corp", n=1, t_invalid=T0 + timedelta(days=9)
    ),
    _fact("Person:sam ortiz", "works_at", "Company:acme corp", n=2),
    _fact("Company:acme corp", "acquired", "Company:globex", n=3),
    _fact("Person:sam ortiz", "works_at", "Company:straße gmbh", n=4),
    _fact("Project:falcon@hr", "owned_by", "Company:acme corp", n=5, acl_ref="acl:hr"),
    _fact("Company:globex", "partnered_with", "Company:acme corp", n=6, acl_ref="acl:finance"),
] + [
    _fact("Company:globex", "acquired", f"Company:hr{n}", n=100 + n, acl_ref="acl:hr")
    for n in range(6)
]

QUERIES = [
    "who works at Acme?",
    "acme acquired",
    "Sammy",
    "strasse",
    "skyhawk falcon",
    "globex partnered",
    "nothing matches this",
]
CALLERS = [
    {"acl_subject": None, "allowed_acls": None},
    {"acl_subject": "user:intern", "allowed_acls": []},
    {"acl_subject": "user:finance", "allowed_acls": ["acl:finance"]},
    {"acl_subject": "user:hr", "allowed_acls": ["acl:hr", "acl:finance"]},
]


async def _load(target: NetworkxStore | Neo4jStore, facts: list[Fact]) -> None:
    for fact in facts:
        await target.add_fact(fact)
    for entity_id, entity_type, name, aliases, acl_ref in ENTITIES:
        await target.upsert_entity(
            Entity(
                id=entity_id,
                type=entity_type,
                canonical_name=name,
                aliases=aliases,
                provenance=_provenance(),
            ),
            acl_ref=acl_ref,
        )


async def test_lexical_search_matches_networkx_exactly(store: Neo4jStore) -> None:
    reference = NetworkxStore()
    await _load(reference, FACTS)
    await _load(store, FACTS)

    for query in QUERIES:
        for caller in CALLERS:
            for as_of in (None, T0 + timedelta(days=1)):
                expected = await reference.search(
                    query,
                    mode="lexical",
                    k=20,
                    as_of=as_of,
                    **caller,  # type: ignore[arg-type]
                )
                actual = await store.search(
                    query,
                    mode="lexical",
                    k=20,
                    as_of=as_of,
                    **caller,  # type: ignore[arg-type]
                )
                assert [f.id for f in actual] == [f.id for f in expected], (query, caller, as_of)


async def test_forbidden_facts_do_not_change_neo4j_ranks(store: Neo4jStore) -> None:
    visible_only = [f for f in FACTS if f.acl_ref is None]
    reference = NetworkxStore()
    await _load(reference, visible_only)
    await _load(store, FACTS)

    for query in QUERIES:
        expected = await reference.search(query, mode="lexical", k=20, allowed_acls=[])
        actual = await store.search(query, mode="lexical", k=20, allowed_acls=[])
        assert [f.id for f in actual] == [f.id for f in expected], query
