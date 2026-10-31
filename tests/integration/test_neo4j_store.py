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
    # Clear any leftover state from a previous test in the module.
    async with store._driver.session(database=store._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")
    await store.initialize()
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


async def test_cross_acl_upsert_raises_in_neo4j(store: Neo4jStore) -> None:
    """#48: cross-ACL re-upsert now raises CrossAclUpsertError in Neo4j
    too; the two-query transaction rolls back so no partial state
    lands."""
    import pytest as _pytest

    from ontos.runtime.models import Entity
    from ontos.storage.base import CrossAclUpsertError

    e_fin = Entity(
        id="alice",
        type="Person",
        canonical_name="Alice Finance",
        properties={"dept": "finance"},
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    e_hr = Entity(
        id="alice",
        type="Person",
        canonical_name="Alice HR",
        properties={"dept": "hr"},
        provenance=e_fin.provenance,
    )
    await store.upsert_entity(e_fin, acl_ref="acl:finance")

    # Downgrade (X → None) raises.
    with _pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(e_fin, acl_ref=None)

    # Cross-label (X → Y) raises.
    with _pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(e_hr, acl_ref="acl:hr")


async def test_neo4j_cross_acl_check_is_atomic(store: Neo4jStore) -> None:
    """#48: after a rejected cross-ACL write, the node's attrs are the
    first-write values — the Neo4j transaction rolled back cleanly, no
    partial update."""
    import json

    import pytest as _pytest

    from ontos.runtime.models import Entity
    from ontos.storage.base import CrossAclUpsertError

    prov = Provenance(
        source_id="doc1",
        extractor_id="test",
        extractor_version="0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )
    await store.upsert_entity(
        Entity(
            id="alice",
            type="Person",
            canonical_name="Alice Finance",
            properties={"dept": "finance"},
            provenance=prov,
        ),
        acl_ref="acl:finance",
    )
    with _pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(
            Entity(
                id="alice",
                type="Person",
                canonical_name="Alice HR",
                properties={"dept": "hr"},
                provenance=prov,
            ),
            acl_ref="acl:hr",
        )

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        result = await session.run("MATCH (n:Entity {id: 'alice'}) RETURN n")
        record = await result.single()
        assert record is not None
        node = record["n"]
        assert node["acl_ref"] == "acl:finance"
        assert node["canonical_name"] == "Alice Finance"
        assert json.loads(node["properties"]) == {"dept": "finance"}


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


async def test_ontology_stamp_round_trips_through_neo4j(store: Neo4jStore) -> None:
    """#34: ontology_id + ontology_version persist on the RELATES edge
    and reconstruct on read. The two new fields are plain string props
    (no index) so they only cost 2 extra bytes per edge."""
    now = datetime.now(UTC)
    prov = Provenance(
        source_id="doc-v01",
        extractor_id="test",
        extractor_version="0.0.0",
        ontology_id="ontos.starter",
        ontology_version="0.1",
        confidence=Confidence.EXTRACTED,
        confidence_score=0.95,
    )
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=prov,
        t_valid=now,
        ingested_at=now,
    )
    await store.add_fact(fact)

    got = await store.get_fact(fact.id)
    assert got is not None
    assert got.provenance.ontology_id == "ontos.starter"
    assert got.provenance.ontology_version == "0.1"


async def test_legacy_edge_without_ontology_props_loads_with_empty_sentinel(
    store: Neo4jStore,
) -> None:
    """A pre-#34 edge in a production store has no prov_ontology_* props.
    `_edge_props_to_fact` must default both fields to "" so the fact
    loads instead of raising KeyError. Simulated by writing a RELATES
    edge directly with the pre-#34 prop set."""
    fact_id = str(uuid4())
    now = datetime.now(UTC)
    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        await session.run(
            """
            MERGE (s:Entity {id: 'legacy:subject'})
            MERGE (o:Entity {id: 'legacy:object'})
            CREATE (s)-[:RELATES {
                fact_id: $fact_id,
                predicate: 'knows',
                t_valid: $now,
                ingested_at: $now,
                prov_source_id: 'legacy-doc',
                prov_extractor_id: 'legacy.extractor',
                prov_extractor_version: '0.0.0',
                prov_confidence: 'EXTRACTED',
                prov_confidence_score: 1.0
            }]->(o)
            """,
            fact_id=fact_id,
            now=now,
        )

    from uuid import UUID

    got = await store.get_fact(UUID(fact_id))
    assert got is not None
    assert got.provenance.ontology_id == ""
    assert got.provenance.ontology_version == ""


# ---------------------------------------------------------------------------
# #32 — backend-neutral graph scan for export
# ---------------------------------------------------------------------------


async def test_scan_preserves_raw_ids_and_stored_entity_attributes(
    store: Neo4jStore,
) -> None:
    from ontos.runtime.models import Entity

    await store.upsert_entity(
        Entity(
            id="person:alice",
            type="Person",
            canonical_name="Alice",
            aliases=["A. Smith"],
            properties={"team": "finance"},
            provenance=_provenance(),
        )
    )
    await store.upsert_entity(
        Entity(
            id="company:acme-1",
            type="Company",
            canonical_name="Acme",
            provenance=_provenance(),
        )
    )

    fact = _make_fact("person:alice", "works_at", "company:acme-1")
    await store.add_fact(fact)

    scan = await store.scan()

    entities = {entity.id: entity for entity in scan.entities}
    assert set(entities) == {"person:alice", "company:acme-1"}
    assert entities["person:alice"].type == "Person"
    assert entities["person:alice"].canonical_name == "Alice"
    assert entities["person:alice"].aliases == ["A. Smith"]
    assert entities["person:alice"].properties == {"team": "finance"}
    assert entities["person:alice"].provenance_source_id == "test-source"
    assert entities["person:alice"].provenance_extractor_id == "test"

    assert len(scan.facts) == 1
    assert scan.facts[0].id == fact.id
    assert scan.facts[0].subject_id == "person:alice"
    assert scan.facts[0].object_id == "company:acme-1"


async def test_scan_temporal_modes(store: Neo4jStore) -> None:
    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme-1",
        t_valid=datetime(2026, 1, 2, tzinfo=UTC),
    )
    await store.add_fact(fact)
    await store.close_fact(
        fact.id,
        t_invalid=datetime(2026, 6, 1, tzinfo=UTC),
    )

    active = await store.scan()
    historical = await store.scan(as_of=datetime(2026, 3, 1, tzinfo=UTC))
    audit = await store.scan(include_closed=True)

    assert active.facts == []
    assert [item.id for item in historical.facts] == [fact.id]
    assert [item.id for item in audit.facts] == [fact.id]

    with pytest.raises(ValueError, match="include_closed"):
        await store.scan(
            as_of=datetime(2026, 3, 1, tzinfo=UTC),
            include_closed=True,
        )


async def test_scan_as_of_excludes_merges_resolved_later(store: Neo4jStore) -> None:
    from ontos.resolver.base import MergeRecord

    for merged_id, resolved_at in (
        ("B", datetime(2026, 1, 3, tzinfo=UTC)),
        ("C", datetime(2027, 1, 1, tzinfo=UTC)),
    ):
        await store.record_merge(
            MergeRecord(
                canonical_id="A",
                merged_ids=[merged_id],
                resolver_id="exact-match",
                resolver_version="1.0",
                resolved_at=resolved_at,
            )
        )

    historical = await store.scan(as_of=datetime(2026, 6, 1, tzinfo=UTC))
    current = await store.scan()

    assert [record.merged_ids for record in historical.merges] == [["B"]]
    assert sorted(mid for record in current.merges for mid in record.merged_ids) == ["B", "C"]


async def test_scan_denies_restricted_entities_and_endpoint_facts_by_default(
    store: Neo4jStore,
) -> None:
    from ontos.runtime.models import Entity

    await store.upsert_entity(
        Entity(
            id="person:alice",
            type="Person",
            canonical_name="Alice Finance",
            provenance=_provenance(),
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        Entity(
            id="company:public",
            type="Company",
            canonical_name="Public Co",
            provenance=_provenance(),
        )
    )

    # Public fact, restricted endpoint: the fact must still be dropped
    # because returning its raw endpoint id would leak restricted existence.
    await store.add_fact(_make_fact("person:alice", "works_at", "company:public"))

    scan = await store.scan()

    assert {entity.id for entity in scan.entities} == {"company:public"}
    assert scan.facts == []


async def test_scan_allows_restricted_graph_when_acl_is_authorized(
    store: Neo4jStore,
) -> None:
    from ontos.runtime.models import Entity

    await store.upsert_entity(
        Entity(
            id="person:alice",
            type="Person",
            canonical_name="Alice Finance",
            provenance=_provenance(),
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        Entity(
            id="company:acme-1",
            type="Company",
            canonical_name="Acme",
            provenance=_provenance(),
        ),
        acl_ref="acl:finance",
    )

    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme-1",
        acl_ref="acl:finance",
    )
    await store.add_fact(fact)

    denied = await store.scan()
    allowed = await store.scan(allowed_acls=["acl:finance"])

    assert denied.entities == []
    assert denied.facts == []

    assert {entity.id for entity in allowed.entities} == {
        "person:alice",
        "company:acme-1",
    }
    assert [item.id for item in allowed.facts] == [fact.id]


async def test_scan_filters_merge_records_and_keeps_raw_ids(
    store: Neo4jStore,
) -> None:
    from ontos.resolver.base import MergeRecord
    from ontos.runtime.models import Entity

    await store.upsert_entity(
        Entity(
            id="company:acme-1",
            type="Company",
            canonical_name="Acme",
            provenance=_provenance(),
        )
    )
    await store.upsert_entity(
        Entity(
            id="company:acme-2",
            type="Company",
            canonical_name="Acme Holdings",
            provenance=_provenance(),
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        Entity(
            id="person:alice",
            type="Person",
            canonical_name="Alice",
            provenance=_provenance(),
        )
    )

    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme-2",
        acl_ref="acl:finance",
    )
    await store.add_fact(fact)

    await store.record_merge(
        MergeRecord(
            canonical_id="company:acme-1",
            merged_ids=["company:acme-2"],
            resolver_id="exact-match",
            resolver_version="1.0",
            resolved_at=datetime(2026, 1, 3, tzinfo=UTC),
            reason="same registration number",
        )
    )

    denied = await store.scan()
    allowed = await store.scan(allowed_acls=["acl:finance"])

    assert denied.merges == []
    assert "company:acme-2" not in {entity.id for entity in denied.entities}

    assert len(allowed.merges) == 1
    assert allowed.merges[0].canonical_id == "company:acme-1"
    assert allowed.merges[0].merged_ids == ["company:acme-2"]

    # Resolver grouping stays separate; the stored fact endpoint remains raw.
    assert allowed.facts[0].object_id == "company:acme-2"


async def test_push_neo4j_visualization_is_idempotent(store: Neo4jStore) -> None:
    from ontos.exporters.cypher import push_neo4j
    from ontos.resolver.base import MergeRecord
    from ontos.storage.models import GraphScan, StoredEntity

    when = datetime(2026, 1, 2, tzinfo=UTC)
    fact = _make_fact(
        "person:alice",
        "works_at",
        "company:acme-1",
        t_valid=when,
    )

    scan = GraphScan(
        entities=[
            StoredEntity(
                id="person:alice",
                type="Person",
                canonical_name="Alice",
            ),
            StoredEntity(
                id="company:acme-1",
                type="Company",
                canonical_name="Acme",
            ),
            StoredEntity(
                id="company:acme-2",
                type="Company",
                canonical_name="Acme Holdings",
            ),
        ],
        facts=[fact],
        merges=[
            MergeRecord(
                canonical_id="company:acme-1",
                merged_ids=["company:acme-2"],
                resolver_id="exact-match",
                resolver_version="1.0",
                resolved_at=datetime(2026, 1, 3, tzinfo=UTC),
                reason="same registration number",
            )
        ],
    )

    await push_neo4j(scan, store._driver, database=store._database)  # noqa: SLF001
    await push_neo4j(scan, store._driver, database=store._database)  # noqa: SLF001

    async with store._driver.session(database=store._database) as session:  # noqa: SLF001
        node_result = await session.run("MATCH (n) RETURN count(n) AS count")
        node_record = await node_result.single()

        fact_result = await session.run(
            "MATCH ()-[r]->() WHERE r.fact_id = $fact_id RETURN count(r) AS count",
            fact_id=str(fact.id),
        )
        fact_record = await fact_result.single()

        merge_result = await session.run("MATCH ()-[r:MERGED_WITH]->() RETURN count(r) AS count")
        merge_record = await merge_result.single()

    assert node_record is not None
    assert fact_record is not None
    assert merge_record is not None
    assert node_record["count"] == 3
    assert fact_record["count"] == 1
    assert merge_record["count"] == 1


def _rejected_candidate(acl_ref: str | None):
    from ontos.extraction import (
        CandidateProvenance,
        CandidateTriple,
        RejectedCandidate,
    )

    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="private-subject",
            subject_type="Person",
            subject_canonical_name="Private Subject",
            predicate="unapproved_relation",
            object_id="private-object",
            object_type="Company",
            object_canonical_name="Private Object",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="restricted-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.9,
        ),
        acl_ref=acl_ref,
    )


async def test_rejected_candidates_acl_and_graph_isolation(
    store: Neo4jStore,
) -> None:
    await store.add_candidates(
        [
            _rejected_candidate("acl:finance"),
            _rejected_candidate("acl:hr"),
            _rejected_candidate(None),
        ]
    )

    assert await store.list_candidates(allowed_acls=[]) == []

    finance = await store.list_candidates(
        allowed_acls=["acl:finance"],
    )
    assert len(finance) == 1
    assert finance[0].acl_ref == "acl:finance"

    public = await store.list_candidates(
        allowed_acls=[],
        include_public=True,
    )
    assert len(public) == 1
    assert public[0].acl_ref is None

    assert (
        len(
            await store.list_candidates(
                allowed_acls=["acl:finance"],
                include_public=True,
            )
        )
        == 2
    )

    assert (
        await store.search(
            "unapproved_relation",
            allowed_acls=["acl:finance"],
        )
        == []
    )

    assert (
        list(
            await store.traverse(
                "private-subject",
                allowed_acls=["acl:finance"],
            )
        )
        == []
    )

    snapshot = await store.scan(include_all_acls=True)
    assert snapshot.entities == []
    assert snapshot.facts == []
    assert snapshot.merges == []

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (n:RejectedCandidate)
            RETURN count(n) AS total,
                   sum(CASE WHEN n:Entity THEN 1 ELSE 0 END) AS entities
            """
        )
        row = await result.single()
        assert row["total"] == 3
        assert row["entities"] == 0


async def test_rejected_candidates_survive_new_store_session(
    store: Neo4jStore,
) -> None:
    await store.add_candidates([_rejected_candidate("acl:finance")])

    # Open a new store wrapper against the same driver/database.
    reader = Neo4jStore(store._driver, database=store._database)

    candidates = await reader.list_candidates(
        allowed_acls=["acl:finance"],
    )
    assert len(candidates) == 1
    assert candidates[0].provenance.source_id == "restricted-source"


async def test_rejected_candidate_writes_are_idempotent(
    store: Neo4jStore,
) -> None:
    candidate = _rejected_candidate("acl:finance")
    other = _rejected_candidate("acl:hr")

    # Duplicate entries within the same batch.
    await store.add_candidates([candidate, candidate, other])

    # Repeated ingestion of the same candidates.
    await store.add_candidates([candidate, other])

    finance = await store.list_candidates(
        allowed_acls=["acl:finance"],
    )
    hr = await store.list_candidates(
        allowed_acls=["acl:hr"],
    )

    assert len(finance) == 1
    assert len(hr) == 1
    assert finance[0] == candidate
    assert hr[0] == other

    # Verify physical storage contains exactly two records.
    async with store._driver.session(database=store._database) as session:
        result = await session.run("MATCH (n:RejectedCandidate) RETURN count(n) AS total")
        row = await result.single()
        assert row is not None
        assert row["total"] == 2


async def test_rejected_candidate_legacy_identity_is_preserved(
    store: Neo4jStore,
) -> None:
    import hashlib
    import json

    from ontos.storage.base import candidate_storage_id

    original = _rejected_candidate("acl:finance")

    # The previous algorithm hashed the complete candidate payload,
    # including both confidence values.
    legacy_payload = json.dumps(
        original.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_id = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()

    logical_id = candidate_storage_id(original)
    assert legacy_id != logical_id

    # Simulate a previously retained candidate with its historical ID.
    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            CREATE (c:RejectedCandidate {
                id: $id,
                source_id: $source_id,
                acl_ref: $acl_ref,
                payload: $payload
            })
            """,
            id=legacy_id,
            source_id=original.provenance.source_id,
            acl_ref=original.acl_ref,
            payload=original.model_dump_json(),
        )
        await result.consume()

    updated_confidence = original.model_copy(
        update={
            "raw_triple": original.raw_triple.model_copy(update={"llm_confidence": 0.51}),
            "provenance": original.provenance.model_copy(update={"confidence_score": 0.51}),
        }
    )

    assert candidate_storage_id(updated_confidence) == logical_id

    # Schema initialization should migrate the logical identity
    # without replacing the historical ID.
    await store.initialize()
    await store.add_candidates([updated_confidence])

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate)
            RETURN c.id AS id, c.logical_id AS logical_id
            """
        )
        rows = await result.data()

    assert rows == [{"id": legacy_id, "logical_id": logical_id}]

    retained = await store.list_candidates(
        allowed_acls=["acl:finance"],
    )

    # First-write-wins: original confidence remains unchanged.
    assert retained == [original]


async def test_rejected_candidate_legacy_physical_id_is_audited(
    store: Neo4jStore,
) -> None:
    import hashlib
    import json

    from ontos.audit.emitter import AuditEmitter
    from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
    from ontos.runtime.candidate_inspection import inspect_candidates
    from ontos.runtime.models import ArticleTwelveField
    from ontos.storage.base import candidate_storage_id

    candidate = _rejected_candidate("acl:finance")

    # Reproduce the historical full-payload identity algorithm.
    legacy_payload = json.dumps(
        candidate.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    historical_id = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()

    logical_id = candidate_storage_id(candidate)
    assert historical_id != logical_id

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            CREATE (c:RejectedCandidate {
                id: $id,
                acl_ref: $acl_ref,
                payload: $payload
            })
            """,
            id=historical_id,
            acl_ref=candidate.acl_ref,
            payload=candidate.model_dump_json(),
        )
        await result.consume()

    await store.initialize()

    # Confirm the storage interface preserves the historical record ID.
    stored_records = await store.list_candidate_records(
        allowed_acls=["acl:finance"],
    )
    assert len(stored_records) == 1
    assert stored_records[0].stored_id == historical_id
    assert stored_records[0].candidate == candidate

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    audit = AuditEmitter(signing_key=b"neo4j-legacy-candidate-audit-test")

    page, record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="ontos-cli",
        identity_assurance="operator_asserted",
        limit=1,
    )

    assert page == [candidate]
    assert len(audit) == 1

    from ontos.runtime.candidate_inspection import (
        CANDIDATE_REFERENCE_KEY_LABEL,
        candidate_audit_reference,
    )

    disclosed_ids = json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
    key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
    assert disclosed_ids == [candidate_audit_reference(key, historical_id)]
    assert disclosed_ids != [candidate_audit_reference(key, logical_id)]
    assert audit.verify_chain()


async def test_rejected_candidate_legacy_logical_identity_collision(
    store: Neo4jStore,
) -> None:
    import hashlib
    import json

    from ontos.storage.base import candidate_storage_id

    first = _rejected_candidate("acl:finance")
    second = first.model_copy(
        update={
            "raw_triple": first.raw_triple.model_copy(update={"llm_confidence": 0.51}),
            "provenance": first.provenance.model_copy(update={"confidence_score": 0.51}),
        }
    )

    assert candidate_storage_id(first) == candidate_storage_id(second)

    historical = []
    for candidate in (first, second):
        serialized = json.dumps(
            candidate.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        old_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        historical.append((old_id, candidate.model_dump_json()))

    assert historical[0][0] != historical[1][0]

    async with store._driver.session(database=store._database) as session:
        for old_id, payload in historical:
            result = await session.run(
                """
                CREATE (c:RejectedCandidate {
                    id: $id,
                    acl_ref: $acl_ref,
                    payload: $payload
                })
                """,
                id=old_id,
                acl_ref="acl:finance",
                payload=payload,
            )
            await result.consume()

    # A collision must be detected before changing either historical record.
    with pytest.raises(
        RuntimeError,
        match="candidate logical identity migration collision",
    ) as exc_info:
        await store.initialize()

    # Error messages must not disclose raw candidate content.
    message = str(exc_info.value)
    assert first.raw_triple.subject_id not in message
    assert first.raw_triple.object_id not in message
    assert first.raw_triple.predicate not in message

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate)
            RETURN c.id AS id,
                   c.payload AS payload,
                   c.logical_id AS logical_id
            ORDER BY c.id
            """
        )
        rows = await result.data()

    expected = sorted(historical, key=lambda item: item[0])

    assert len(rows) == 2
    assert [(row["id"], row["payload"]) for row in rows] == expected
    assert all(row["logical_id"] is None for row in rows)


async def test_neo4j_candidate_write_transaction_rolls_back(
    store: Neo4jStore,
) -> None:
    """A failed managed write transaction must undo preceding writes."""
    candidate = _rejected_candidate("acl:finance")

    await store.add_candidates([candidate])

    from ontos.storage.base import candidate_storage_id

    logical_id = candidate_storage_id(candidate)

    async def failing_transaction(tx):
        result = await tx.run(
            """
            MATCH (c:RejectedCandidate {logical_id: $logical_id})
            SET c.source_id = 'temporary-migration-change'
            """,
            logical_id=logical_id,
        )
        await result.consume()

        raise RuntimeError("injected rollback test failure")

    async with store._driver.session(database=store._database) as session:
        with pytest.raises(RuntimeError, match="injected rollback test failure"):
            await session.execute_write(failing_transaction)

    records = await store.list_candidate_records(
        allowed_acls=["acl:finance"],
    )
    assert len(records) == 1
    assert records[0].candidate == candidate

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate {logical_id: $logical_id})
            RETURN c.source_id AS source_id
            """,
            logical_id=logical_id,
        )
        row = await result.single()

    assert row is not None
    assert row["source_id"] == candidate.provenance.source_id


async def test_rejected_candidate_legacy_collides_with_migrated(
    store: Neo4jStore,
) -> None:
    import hashlib
    import json

    existing = _rejected_candidate("acl:finance")
    legacy = existing.model_copy(
        update={
            "raw_triple": existing.raw_triple.model_copy(update={"llm_confidence": 0.51}),
            "provenance": existing.provenance.model_copy(update={"confidence_score": 0.51}),
        }
    )

    await store.add_candidates([existing])

    serialized = json.dumps(
        legacy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    historical_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            CREATE (c:RejectedCandidate {
                id: $id,
                acl_ref: $acl_ref,
                payload: $payload
            })
            """,
            id=historical_id,
            acl_ref=legacy.acl_ref,
            payload=legacy.model_dump_json(),
        )
        await result.consume()

    with pytest.raises(
        RuntimeError,
        match="candidate logical identity migration collision",
    ) as exc_info:
        await store.initialize()

    message = str(exc_info.value)
    assert existing.raw_triple.subject_id not in message
    assert existing.raw_triple.predicate not in message

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate)
            RETURN c.id AS id, c.logical_id AS logical_id
            """
        )
        rows = await result.data()

    assert len(rows) == 2
    assert historical_id in [row["id"] for row in rows]
    assert next(row for row in rows if row["id"] == historical_id)["logical_id"] is None


async def test_rejected_candidate_legacy_cursor_pagination(
    store: Neo4jStore,
) -> None:
    import hashlib
    import json

    from ontos.storage.base import (
        candidate_storage_id,
        encode_candidate_cursor,
    )

    original = _rejected_candidate("acl:finance")

    candidates = [
        original.model_copy(
            update={
                "provenance": original.provenance.model_copy(
                    update={"source_id": f"legacy-source-{i:03d}"}
                )
            }
        )
        for i in range(3)
    ]

    historical_ids = []

    async with store._driver.session(database=store._database) as session:
        for candidate in candidates:
            payload = json.dumps(
                candidate.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            historical_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()

            assert historical_id != candidate_storage_id(candidate)
            historical_ids.append(historical_id)

            result = await session.run(
                """
                CREATE (c:RejectedCandidate {
                    id: $id,
                    acl_ref: $acl_ref,
                    payload: $payload
                })
                """,
                id=historical_id,
                acl_ref=candidate.acl_ref,
                payload=candidate.model_dump_json(),
            )
            await result.consume()

    await store.initialize()

    collected = []
    cursor = None

    while True:
        page = await store.list_candidates(
            allowed_acls=["acl:finance"],
            limit=1,
            after=cursor,
        )
        if not page:
            break

        collected.extend(page)
        cursor = encode_candidate_cursor(page[-1])

    assert collected == candidates

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate)
            RETURN c.id AS id
            ORDER BY c.source_id
            """
        )
        rows = await result.data()

    assert [row["id"] for row in rows] == historical_ids


async def test_rejected_candidate_legacy_source_id_migration(
    store: Neo4jStore,
) -> None:
    from ontos.storage.base import candidate_storage_id

    candidate = _rejected_candidate("acl:finance")
    candidate_id = candidate_storage_id(candidate)

    # Simulate the earlier storage schema, which had no source_id property.
    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            CREATE (c:RejectedCandidate {
                id: $id,
                acl_ref: $acl_ref,
                payload: $payload
            })
            """,
            id=candidate_id,
            acl_ref=candidate.acl_ref,
            payload=candidate.model_dump_json(),
        )
        await result.consume()

    await store.initialize()

    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (c:RejectedCandidate {id: $id})
            RETURN c.source_id AS source_id
            """,
            id=candidate_id,
        )
        row = await result.single()

    assert row is not None
    assert row["source_id"] == candidate.provenance.source_id

    visible = await store.list_candidates(
        allowed_acls=["acl:finance"],
        limit=1,
    )
    assert candidate in visible

    # Re-running the migration must be harmless.
    await store.initialize()

    visible_again = await store.list_candidates(
        allowed_acls=["acl:finance"],
        limit=1,
    )
    assert visible_again == visible


async def test_rejected_candidate_cursor_pagination(
    store: Neo4jStore,
) -> None:
    from ontos.storage.base import encode_candidate_cursor

    original = _rejected_candidate("acl:finance")

    candidates = [
        original.model_copy(
            update={
                "provenance": original.provenance.model_copy(
                    update={"source_id": f"source-{i:03d}"}
                )
            }
        )
        for i in range(7)
    ]

    forbidden = original.model_copy(
        update={
            "acl_ref": "acl:hr",
            "provenance": original.provenance.model_copy(update={"source_id": "source-003a"}),
        }
    )

    await store.add_candidates(list(reversed(candidates)) + [forbidden])

    collected = []
    after = None

    while True:
        page = await store.list_candidates(
            allowed_acls=["acl:finance"],
            limit=2,
            after=after,
        )
        if not page:
            break

        collected.extend(page)
        after = encode_candidate_cursor(page[-1])

    assert [item.provenance.source_id for item in collected] == [
        f"source-{i:03d}" for i in range(7)
    ]
    assert forbidden not in collected


async def test_rejected_candidate_limit_applied_after_acl_filter(
    store: Neo4jStore,
) -> None:
    await store.add_candidates(
        [
            _rejected_candidate("acl:hr"),
            _rejected_candidate("acl:finance"),
            _rejected_candidate("acl:finance"),
        ]
    )

    candidates = await store.list_candidates(
        allowed_acls=["acl:finance"],
        limit=1,
    )
    assert len(candidates) == 1
    assert candidates[0].acl_ref == "acl:finance"

    with pytest.raises(ValueError, match="limit"):
        await store.list_candidates(
            allowed_acls=["acl:finance"],
            limit=0,
        )


async def test_rejected_candidate_pagination_matches_networkx_for_non_bmp_sources(
    store: Neo4jStore,
) -> None:
    """Neo4j orders raw non-BMP strings differently from its own `>` cursor
    comparison, which silently skipped candidates. Paging must return every
    candidate exactly once, in the same order as NetworkX."""
    from ontos.storage.base import encode_candidate_cursor
    from ontos.storage.networkx_store import NetworkxStore

    template = _rejected_candidate("acl:finance")
    sources = ["￿", "\U0001f600", "Ａ-doc", "\U00020000", "ascii-doc", ""]
    candidates = [
        template.model_copy(
            update={
                "raw_triple": template.raw_triple.model_copy(update={"subject_id": f"s-{i}"}),
                "provenance": template.provenance.model_copy(update={"source_id": source}),
            }
        )
        for i, source in enumerate(sources * 2)
    ]

    reference = NetworkxStore()
    await reference.add_candidates(candidates)
    await store.add_candidates(candidates)

    async def page_all(backend: NetworkxStore | Neo4jStore) -> list[str]:
        seen: list[str] = []
        after: str | None = None
        while True:
            records = await backend.list_candidate_records(
                allowed_acls=["acl:finance"], limit=2, after=after
            )
            page = records[:1]
            seen += [record.stored_id for record in page]
            if len(records) <= 1:
                return seen
            after = encode_candidate_cursor(page[-1].candidate)

    expected = await page_all(reference)
    assert len(expected) == len(candidates)
    assert await page_all(store) == expected


async def test_get_visible_fact_applies_acl_in_query(
    store: Neo4jStore,
    sample_fact: Fact,
) -> None:
    """#78: the read-path fact lookup filters by ACL inside the query."""
    from uuid import uuid4

    restricted = sample_fact.model_copy(update={"acl_ref": "acl:finance"})
    public = sample_fact.model_copy(update={"id": uuid4(), "acl_ref": None})
    await store.add_fact(restricted)
    await store.add_fact(public)

    assert await store.get_visible_fact(restricted.id, allowed_acls=["acl:finance"]) is not None
    assert await store.get_visible_fact(restricted.id, allowed_acls=["acl:legal"]) is None
    assert await store.get_visible_fact(restricted.id) is None
    assert await store.get_visible_fact(restricted.id, acl_subject="acl:finance") is not None
    assert await store.get_visible_fact(uuid4(), allowed_acls=["acl:finance"]) is None
    assert await store.get_visible_fact(public.id) is not None
    # The write path still sees everything.
    assert await store.get_fact(restricted.id) is not None


async def test_add_candidates_returns_number_newly_created(store: Neo4jStore) -> None:
    """#73: lets the pipeline report retained candidates without counting
    duplicates."""
    first = _rejected_candidate("acl:finance")
    second = first.model_copy(
        update={"raw_triple": first.raw_triple.model_copy(update={"subject_id": "other"})}
    )

    assert await store.add_candidates([first, second, first]) == 2
    assert await store.add_candidates([first, second]) == 0
    assert await store.add_candidates([]) == 0
    with pytest.raises(ValueError):
        await store.list_candidate_records(allowed_acls=[], limit=True)


async def _neo4j_atlas_graph(store: Neo4jStore, sample_fact: Fact) -> None:
    from uuid import uuid4

    from ontos.runtime.models import Entity

    def entity(entity_id: str, type_: str, name: str, aliases: list[str]) -> Entity:
        return Entity(
            id=entity_id,
            type=type_,
            canonical_name=name,
            aliases=aliases,
            provenance=sample_fact.provenance,
        )

    await store.upsert_entity(entity("0", "Person", "Maya Chen", []))
    await store.upsert_entity(entity("1", "Person", "Daniel Foster", []))
    await store.upsert_entity(entity("2", "Company", "Atlas Robotics", ["Atlas"]))
    for person in ("0", "1"):
        await store.add_fact(
            sample_fact.model_copy(
                update={
                    "id": uuid4(),
                    "subject_id": person,
                    "predicate": "works_at",
                    "object_id": "2",
                }
            )
        )


async def test_search_matches_names_aliases_and_ids(
    store: Neo4jStore,
    sample_fact: Fact,
) -> None:
    """#82: same matching rule as NetworkxStore — ids, predicate, and the
    names/aliases of visible endpoint entities."""
    await _neo4j_atlas_graph(store, sample_fact)

    assert sorted(f.subject_id for f in await store.search("Atlas Robotics")) == ["0", "1"]
    assert len(await store.search("atlas")) == 2
    assert [f.subject_id for f in await store.search("maya chen")] == ["0"]
    assert len(await store.search("works_at")) == 2
    # Ids match too (they did not on Neo4j before #82).
    assert len(await store.search("2")) == 2


async def test_search_does_not_match_names_of_invisible_entities(
    store: Neo4jStore,
    sample_fact: Fact,
) -> None:
    from uuid import uuid4

    from ontos.runtime.models import Entity

    public_fact = sample_fact.model_copy(
        update={"id": uuid4(), "subject_id": "p", "predicate": "works_at", "object_id": "c"}
    )
    await store.add_fact(public_fact)
    # The raw public node "c" is then claimed by a restricted document (None -> X).
    await store.upsert_entity(
        Entity(
            id="c",
            type="Company",
            canonical_name="Secret Corp",
            provenance=sample_fact.provenance,
        ),
        acl_ref="acl:hr",
    )

    assert await store.search("Secret Corp", allowed_acls=[]) == []
    assert len(await store.search("Secret Corp", allowed_acls=["acl:hr"])) == 1


async def test_search_entities_requires_a_visible_fact(
    store: Neo4jStore,
    sample_fact: Fact,
) -> None:
    from uuid import uuid4

    await _neo4j_atlas_graph(store, sample_fact)
    await store.add_fact(
        sample_fact.model_copy(
            update={
                "id": uuid4(),
                "subject_id": "Classified Person",
                "predicate": "works_at",
                "object_id": "Hidden Lab",
                "acl_ref": "acl:secret",
            }
        )
    )

    assert [e.id for e in await store.search_entities("Atlas Robotics")] == ["2"]
    assert [e.id for e in await store.search_entities("ATLAS")] == ["2"]
    # Raw nodes reachable only through forbidden facts must stay hidden.
    assert await store.search_entities("Hidden Lab", allowed_acls=["acl:public"]) == []
    found = await store.search_entities("Hidden Lab", allowed_acls=["acl:secret"])
    assert [e.id for e in found] == ["Hidden Lab"]


async def test_read_paths_hide_facts_with_invisible_endpoint_entities(
    store: Neo4jStore,
    sample_fact: Fact,
) -> None:
    """A public fact pointing at an entity a restricted document later claimed
    must be hidden from every read path, as scan() already does."""
    from uuid import uuid4

    from ontos.runtime.models import Entity

    await store.upsert_entity(
        Entity(id="p", type="Person", canonical_name="Pat", provenance=sample_fact.provenance)
    )
    public = sample_fact.model_copy(
        update={"id": uuid4(), "subject_id": "p", "predicate": "works_at", "object_id": "sc"}
    )
    await store.add_fact(public)
    await store.upsert_entity(
        Entity(
            id="sc", type="Company", canonical_name="Secret Co", provenance=sample_fact.provenance
        ),
        acl_ref="acl:hr",
    )

    for allowed, visible in (([], False), (["acl:hr"], True)):
        got = {
            "search": [f.object_id for f in await store.search("works_at", allowed_acls=allowed)],
            "traverse": [f.object_id for f in await store.traverse("p", allowed_acls=allowed)],
            "facts_for_entity": [
                f.object_id for f in await store.facts_for_entity("p", allowed_acls=allowed)
            ],
            "get_visible_fact": [
                f.object_id
                for f in [await store.get_visible_fact(public.id, allowed_acls=allowed)]
                if f is not None
            ],
            "search_entities": [
                e.id for e in await store.search_entities("Pat", allowed_acls=allowed)
            ],
        }
        for name, ids in got.items():
            expected = (["p"] if name == "search_entities" else ["sc"]) if visible else []
            assert ids == expected, (allowed, name, ids)

    assert (await store.scan(allowed_acls=[])).facts == []
