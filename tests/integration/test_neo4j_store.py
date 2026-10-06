"""Integration tests: Neo4jStore against a real Neo4j 5 via Testcontainers.

Skipped by default (marker `integration`); CI runs them explicitly.
Requires Docker to be reachable — the fixture will skip cleanly if not.

These tests mirror `tests/unit/test_storage.py` intentionally: any behavior
the NetworkX store exhibits must also hold on Neo4j, so a bug that breaks
the shared contract fails in both suites.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def neo4j_container():
    """One Neo4j container per test module — spun up once, torn down at end.

    Skips the whole module if Docker is not available on this host.
    """
    docker = pytest.importorskip("docker")
    testcontainers_neo4j = pytest.importorskip("testcontainers.neo4j")
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 — skip on any docker connectivity failure
        pytest.skip(f"Docker not reachable: {exc}")

    container = testcontainers_neo4j.Neo4jContainer("neo4j:5.24")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def store(neo4j_container) -> AsyncIterator[Neo4jStore]:
    """Fresh Neo4jStore per test with an isolated data set.

    Uses a MATCH-DELETE between tests instead of container-per-test so the
    module runs in seconds, not minutes.
    """
    uri = neo4j_container.get_connection_url()
    password = neo4j_container.password
    store = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await store.initialize()
    # Clear any leftover state from a previous test in the module.
    async with store._driver.session(database=store._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield store
    finally:
        await store.close()


def _provenance() -> Provenance:
    return Provenance(
        source_id="test-source",
        extractor_id="test",
        extractor_version="0.0.0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


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
        provenance=_provenance(),
        t_valid=t_valid or now,
        ingested_at=now,
        acl_ref=acl_ref,
    )


async def test_add_and_get(store: Neo4jStore) -> None:
    fact = _make_fact("A", "knows", "B")
    await store.add_fact(fact)
    got = await store.get_fact(fact.id)
    assert got is not None
    assert got.id == fact.id
    assert got.subject_id == "A"
    assert got.object_id == "B"
    assert got.predicate == "knows"
    assert got.provenance.confidence == Confidence.EXTRACTED


async def test_search_substring_match(store: Neo4jStore) -> None:
    await store.add_fact(_make_fact("Acme Corp", "acquired", "Widget Inc"))
    hits = await store.search("acquired", k=5)
    assert len(hits) == 1
    assert hits[0].predicate == "acquired"


async def test_search_excludes_closed_facts(store: Neo4jStore) -> None:
    fact = _make_fact("A", "reports_to", "B")
    await store.add_fact(fact)
    await store.close_fact(fact.id, t_invalid=datetime.now(UTC), superseded_by=uuid4())
    assert await store.search("reports_to") == []


async def test_as_of_returns_historical_facts(store: Neo4jStore) -> None:
    past = datetime.now(UTC) - timedelta(days=30)
    fact = _make_fact("A", "reports_to", "B", t_valid=past)
    await store.add_fact(fact)
    later = datetime.now(UTC)
    await store.close_fact(fact.id, t_invalid=later)

    when = datetime.now(UTC) - timedelta(days=15)
    hits = await store.search("reports_to", as_of=when)
    assert len(hits) == 1


async def test_close_fact_records_supersession(store: Neo4jStore) -> None:
    fact = _make_fact("A", "reports_to", "B")
    new_fact_id = uuid4()
    await store.add_fact(fact)
    await store.close_fact(fact.id, t_invalid=datetime.now(UTC), superseded_by=new_fact_id)
    got = await store.get_fact(fact.id)
    assert got is not None
    assert got.superseded_by == new_fact_id
    assert got.t_invalid is not None


async def test_traverse_respects_depth(store: Neo4jStore) -> None:
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("B", "knows", "C"))
    await store.add_fact(_make_fact("C", "knows", "D"))

    depth1 = list(await store.traverse("A", depth=1))
    depth2 = list(await store.traverse("A", depth=2))
    assert len(depth1) == 1
    assert len(depth2) == 2


async def test_traverse_filters_by_relation(store: Neo4jStore) -> None:
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("A", "works_at", "C"))
    knows_only = list(await store.traverse("A", depth=1, relation="knows"))
    assert len(knows_only) == 1
    assert knows_only[0].predicate == "knows"


async def test_traverse_incoming_direction(store: Neo4jStore) -> None:
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


async def test_incoming_traversal_does_not_cross_forbidden_acl(
    store: Neo4jStore,
) -> None:
    await store.add_fact(
        _make_fact(
            "person:alice",
            "works_at",
            "company:acme",
            acl_ref="acl:confidential",
        )
    )

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
    store: Neo4jStore,
) -> None:
    await store.add_fact(
        _make_fact(
            "person:alice",
            "works_at",
            "company:acme",
            acl_ref="acl:confidential",
        )
    )
    await store.add_fact(
        _make_fact(
            "person:alice",
            "knows",
            "person:bob",
        )
    )

    results = list(
        await store.traverse(
            "company:acme",
            direction="both",
            depth=2,
            allowed_acls=["acl:public"],
        )
    )

    assert results == []


async def test_incoming_traversal_respects_as_of(store: Neo4jStore) -> None:
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


async def test_acl_blocks_forbidden_facts_from_search(store: Neo4jStore) -> None:
    public = _make_fact("A", "knows", "B")
    private = _make_fact("A", "knows", "C", acl_ref="alice")
    await store.add_fact(public)
    await store.add_fact(private)

    alice_hits = await store.search("knows", acl_subject="alice")
    assert len(alice_hits) == 2
    bob_hits = await store.search("knows", acl_subject="bob")
    assert len(bob_hits) == 1
    assert bob_hits[0].object_id == "B"


async def test_acl_prunes_forbidden_paths_during_traversal(store: Neo4jStore) -> None:
    """The load-bearing invariant: a forbidden edge in the middle of a path
    MUST drop the whole path so the target node never leaks, not even as
    a count or a hidden marker. Verified against real Cypher, not the
    NetworkX approximation."""
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("B", "knows", "C", acl_ref="alice"))

    bob_view = list(await store.traverse("A", depth=3, acl_subject="bob"))
    # Bob should see A→B only. No mention of C anywhere in the result.
    for fact in bob_view:
        assert fact.subject_id != "C"
        assert fact.object_id != "C"


@pytest.mark.parametrize("depth", [1, 5])
async def test_traverse_returns_empty_for_unknown_start(store: Neo4jStore, depth: int) -> None:
    result = list(await store.traverse("nonexistent-entity", depth=depth))
    assert result == []


async def test_facts_for_entity_returns_both_directions(store: Neo4jStore) -> None:
    await store.add_fact(_make_fact("A", "knows", "B"))
    await store.add_fact(_make_fact("C", "manages", "A"))
    facts = await store.facts_for_entity("A")
    assert len(facts) == 2
    predicates = {f.predicate for f in facts}
    assert predicates == {"knows", "manages"}


async def test_traversal_depth_is_capped(store: Neo4jStore) -> None:
    """A malformed caller asking for depth=99999 must not send Cypher that
    tries to actually walk 99999 hops. The store caps at _MAX_TRAVERSAL_DEPTH."""
    # Build a small graph — the cap concerns query construction, not results.
    await store.add_fact(_make_fact("A", "knows", "B"))
    result = list(await store.traverse("A", depth=99_999))
    # Should return the one edge without erroring out; cap silently applied.
    assert len(result) == 1


async def test_initialize_is_idempotent(store: Neo4jStore) -> None:
    # Calling initialize a second time must not raise (indexes have IF NOT EXISTS).
    await store.initialize()
    await store.initialize()


# ---------------------------------------------------------------------------
# #38 — upsert_entity + entity_type_idx
# ---------------------------------------------------------------------------


async def test_upsert_entity_writes_attrs_including_properties_as_json(
    store: Neo4jStore,
) -> None:
    """#38 PR-review regression: `properties` is a dict, and Neo4j rejects
    map-shaped node properties. Must be JSON-encoded on write and decode
    cleanly."""
    import json

    from ontos.runtime.models import Entity

    e = Entity(
        id="alice",
        type="Person",
        canonical_name="Alice",
        aliases=["Alice W."],
        properties={"email": "alice@example.com", "team": "research"},
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    await store.upsert_entity(e, acl_ref="acl:finance")

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("MATCH (n:Entity {id: 'alice'}) RETURN n")
        record = await result.single()
        assert record is not None
        node = record["n"]
        assert node["type"] == "Person"
        assert node["canonical_name"] == "Alice"
        assert list(node["aliases"]) == ["Alice W."]
        # properties stored as JSON string, decodable back to dict
        assert json.loads(node["properties"]) == {
            "email": "alice@example.com",
            "team": "research",
        }
        assert node["acl_ref"] == "acl:finance"


async def test_upsert_entity_is_idempotent_in_neo4j(store: Neo4jStore) -> None:
    from ontos.runtime.models import Entity

    e = Entity(
        id="alice",
        type="Person",
        canonical_name="Alice",
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    await store.upsert_entity(e)
    await store.upsert_entity(e)

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("MATCH (n:Entity {id: 'alice'}) RETURN count(n) AS c")
        record = await result.single()
        assert record is not None
        assert record["c"] == 1


async def test_upsert_entity_acl_is_first_write_wins_in_neo4j(
    store: Neo4jStore,
) -> None:
    """A later public ingest must not downgrade a prior restricted stamp."""
    from ontos.runtime.models import Entity

    e = Entity(
        id="alice",
        type="Person",
        canonical_name="Alice",
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    await store.upsert_entity(e, acl_ref="acl:finance")
    await store.upsert_entity(e, acl_ref=None)

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("MATCH (n:Entity {id: 'alice'}) RETURN n.acl_ref AS acl")
        record = await result.single()
        assert record is not None
        assert record["acl"] == "acl:finance"


async def test_entity_type_idx_exists_after_initialize(store: Neo4jStore) -> None:
    """#38 adds an index on (:Entity).type so #32's type-filtered reads
    stay O(matching-nodes). Verify it exists after initialize()."""
    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("SHOW INDEXES WHERE name = 'entity_type_idx'")
        records = [r async for r in result]
        assert len(records) == 1


# ---------------------------------------------------------------------------
# #46 — record_merge + [:MERGED_WITH] + merge_record_idx
# ---------------------------------------------------------------------------


async def test_record_merge_creates_merged_with_edges_per_non_self_id(
    store: Neo4jStore,
) -> None:
    """One MergeRecord with 3 merged_ids (one is the canonical) → 2
    [:MERGED_WITH] edges, each carrying the audit metadata."""
    from datetime import UTC, datetime

    from ontos.resolver.base import MergeRecord

    rec = MergeRecord(
        canonical_id="A",
        merged_ids=["A", "B", "C"],
        resolver_id="ontos.resolver.exact-match",
        resolver_version="0.1.0",
        resolved_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        reason="exact match on (type, canonical_name)",
    )
    await store.record_merge(rec)

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run(
            "MATCH ()-[r:MERGED_WITH]->(canonical:Entity {id: 'A'}) "
            "RETURN r.resolver_id AS resolver_id, r.reason AS reason, "
            "       startNode(r).id AS merged_id"
        )
        rows = [r async for r in result]
    merged_ids = sorted(row["merged_id"] for row in rows)
    assert merged_ids == ["B", "C"]  # A → A self-loop skipped
    assert all(row["resolver_id"] == "ontos.resolver.exact-match" for row in rows)


async def test_record_merge_is_idempotent_in_neo4j(store: Neo4jStore) -> None:
    """Re-recording the same (canonical, merged, resolver) overwrites
    resolved_at but leaves one edge, not two."""
    from datetime import UTC, datetime

    from ontos.resolver.base import MergeRecord

    def _rec(resolved_at: datetime, reason: str) -> MergeRecord:
        return MergeRecord(
            canonical_id="A",
            merged_ids=["A", "B"],
            resolver_id="ontos.resolver.exact-match",
            resolver_version="0.1.0",
            resolved_at=resolved_at,
            reason=reason,
        )

    await store.record_merge(_rec(datetime(2026, 1, 1, tzinfo=UTC), "first"))
    await store.record_merge(_rec(datetime(2026, 6, 1, tzinfo=UTC), "re-affirmed"))

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run(
            "MATCH ()-[r:MERGED_WITH]->(canonical:Entity {id: 'A'}) "
            "RETURN count(r) AS c, collect(r.reason) AS reasons"
        )
        record = await result.single()
        assert record is not None
        assert record["c"] == 1
        assert record["reasons"] == ["re-affirmed"]


async def test_merges_for_entity_returns_grouped_record_from_either_endpoint(
    store: Neo4jStore,
) -> None:
    """Audit lookup from both canonical and merged ids returns the full
    MergeRecord reconstructed by grouping edges that share (canonical,
    resolver_id, resolved_at)."""
    from datetime import UTC, datetime

    from ontos.resolver.base import MergeRecord

    rec = MergeRecord(
        canonical_id="A",
        merged_ids=["A", "B", "C"],
        resolver_id="ontos.resolver.exact-match",
        resolver_version="0.1.0",
        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason="exact match",
    )
    await store.record_merge(rec)

    from_canonical = await store.merges_for_entity("A")
    assert len(from_canonical) == 1
    assert sorted(from_canonical[0].merged_ids) == ["B", "C"]

    from_merged = await store.merges_for_entity("B")
    assert len(from_merged) == 1
    assert from_merged[0].canonical_id == "A"
    # PR #47 blocking regression: lookup from a merged endpoint MUST
    # return the full group, not just the queried id. Pre-fix the
    # one-step Cypher truncated to ["B"] (losing C) — a real Article-12
    # audit gap. Two-step Cypher finds the group first, then collects
    # all its edges.
    assert sorted(from_merged[0].merged_ids) == ["B", "C"]


async def test_merges_for_entity_pre_filters_by_allowed_acls(
    store: Neo4jStore,
) -> None:
    """PR #47 suggestion 2: optional allowed_acls drops records whose
    endpoint entities have an acl_ref the caller cannot see."""
    from datetime import UTC, datetime

    from ontos.resolver.base import MergeRecord
    from ontos.runtime.models import Confidence, Entity, Provenance

    prov = Provenance(
        source_id="doc",
        extractor_id="t",
        extractor_version="0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )
    # Public canonical, restricted merged endpoint.
    await store.upsert_entity(
        Entity(id="A", type="Person", canonical_name="Alice", provenance=prov),
        acl_ref=None,
    )
    await store.upsert_entity(
        Entity(id="B", type="Person", canonical_name="Alice", provenance=prov),
        acl_ref="acl:finance",
    )
    await store.record_merge(
        MergeRecord(
            canonical_id="A",
            merged_ids=["A", "B"],
            resolver_id="r",
            resolver_version="0",
            resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    hits_public = await store.merges_for_entity("A", allowed_acls=["acl:public"])
    assert hits_public == []

    hits_privileged = await store.merges_for_entity("A", allowed_acls=["acl:public", "acl:finance"])
    assert len(hits_privileged) == 1

    # None = no filter (back-compat for pre-#47 callers).
    hits_unfiltered = await store.merges_for_entity("A")
    assert len(hits_unfiltered) == 1


async def test_merge_record_idx_exists_after_initialize(store: Neo4jStore) -> None:
    """#46 adds an index on [:MERGED_WITH](resolver_id, resolved_at) so
    audit queries stay fast on large graphs."""
    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("SHOW INDEXES WHERE name = 'merge_record_idx'")
        records = [r async for r in result]
        assert len(records) == 1
