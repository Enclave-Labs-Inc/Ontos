"""#38: GraphStore.upsert_entity — unit coverage for NetworkxStore.

Verifies the Protocol method, the attrs that land on a node, idempotence,
last-write-wins + type-overwrite warning, provenance pointers, dirty-flag
regression (PR #41), and pipeline wiring.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _entity(
    eid: str = "alice",
    etype: str = "Person",
    canonical_name: str = "Alice",
    aliases: list[str] | None = None,
    properties: dict[str, str] | None = None,
) -> Entity:
    return Entity(
        id=eid,
        type=etype,
        canonical_name=canonical_name,
        aliases=aliases or [],
        properties=properties or {},
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test-extractor",
            extractor_version="0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )


def test_networkx_store_has_upsert_entity_method() -> None:
    """Protocol change is additive — NetworkxStore implements the new
    `upsert_entity` method. (`GraphStore` is not `@runtime_checkable`
    so `isinstance` doesn't apply; checking the method's presence
    directly is the equivalent structural guard.)"""
    store = NetworkxStore()
    assert hasattr(store, "upsert_entity")
    assert callable(store.upsert_entity)


async def test_upsert_entity_writes_all_attrs() -> None:
    store = NetworkxStore()
    e = _entity(
        aliases=["Alice W.", "A. Wonderland"],
        properties={"email": "alice@example.com", "team": "research"},
    )
    await store.upsert_entity(e)

    attrs = store._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["type"] == "Person"
    assert attrs["canonical_name"] == "Alice"
    assert attrs["aliases"] == ["Alice W.", "A. Wonderland"]
    assert attrs["properties"] == {"email": "alice@example.com", "team": "research"}


async def test_upsert_entity_stores_provenance_pointers() -> None:
    store = NetworkxStore()
    await store.upsert_entity(_entity())
    attrs = store._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["provenance_source_id"] == "doc1"
    assert attrs["provenance_extractor_id"] == "test-extractor"


async def test_upsert_entity_is_idempotent() -> None:
    """MERGE semantics — two upserts of the same id leave one node."""
    store = NetworkxStore()
    await store.upsert_entity(_entity())
    await store.upsert_entity(_entity())
    assert store._graph.number_of_nodes() == 1  # noqa: SLF001


async def test_upsert_entity_last_write_wins_on_attr_change() -> None:
    store = NetworkxStore()
    await store.upsert_entity(_entity(canonical_name="Alice", aliases=["A."]))
    await store.upsert_entity(_entity(canonical_name="Alice Updated", aliases=["AU"]))
    attrs = store._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["canonical_name"] == "Alice Updated"
    assert attrs["aliases"] == ["AU"]


async def test_upsert_entity_logs_warning_on_type_overwrite() -> None:
    """Resolver-bug canary: silent type change is a scary thing. Loud
    structlog line so operators can audit it. Uses
    `structlog.testing.capture_logs` because structlog doesn't route
    through python's logging by default."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(etype="Person"))
    with structlog.testing.capture_logs() as logs:
        await store.upsert_entity(_entity(etype="Company"))
    # Last-write still wins — the warning doesn't block the write.
    assert store._graph.nodes["alice"]["type"] == "Company"  # noqa: SLF001
    overwrites = [log for log in logs if log.get("event") == "entity-type-overwrite"]
    assert len(overwrites) == 1
    assert overwrites[0]["previous_type"] == "Person"
    assert overwrites[0]["new_type"] == "Company"
    assert overwrites[0]["entity_id"] == "alice"


async def test_upsert_entity_no_warning_on_first_write() -> None:
    """First-time upsert of an id is the common path — no warning."""
    store = NetworkxStore()
    with structlog.testing.capture_logs() as logs:
        await store.upsert_entity(_entity())
    assert not any(log.get("event") == "entity-type-overwrite" for log in logs)


async def test_upsert_entity_no_warning_on_same_type_rewrite() -> None:
    """Re-ingesting the same source should not trigger the audit warning."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(etype="Person", canonical_name="Alice"))
    with structlog.testing.capture_logs() as logs:
        await store.upsert_entity(_entity(etype="Person", canonical_name="Alice Updated"))
    assert not any(log.get("event") == "entity-type-overwrite" for log in logs)


async def test_upsert_entity_sets_dirty_flag() -> None:
    """Regression guard for the PR #41 read-only-close invariant: upsert
    must mark dirty so a subsequent flush actually writes."""
    store = NetworkxStore()
    assert store._dirty is False  # noqa: SLF001
    await store.upsert_entity(_entity())
    assert store._dirty is True  # noqa: SLF001


async def test_upsert_entity_then_add_fact_against_same_id() -> None:
    """Common pipeline path: upsert, then add a fact referencing the id.
    No new node should be created by add_fact; it merges onto the typed
    node already present."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(eid="alice", etype="Person"))
    await store.upsert_entity(_entity(eid="acme", etype="Company", canonical_name="Acme"))
    fact = Fact(
        subject_id="alice",
        predicate="works_at",
        object_id="acme",
        provenance=_entity().provenance,
        t_valid=datetime(2026, 1, 1),
        ingested_at=datetime(2026, 1, 1),
    )
    await store.add_fact(fact)
    assert store._graph.number_of_nodes() == 2  # noqa: SLF001
    assert store._graph.nodes["alice"]["type"] == "Person"  # noqa: SLF001
    assert store._graph.nodes["acme"]["type"] == "Company"  # noqa: SLF001


async def test_pipeline_calls_upsert_entity_before_add_fact() -> None:
    """E2E: IngestPipeline threads extracted entities through upsert_entity
    for every doc. Covers the #38 wiring in `ontos/pipeline/ingest.py`."""
    from pathlib import Path
    from unittest.mock import AsyncMock

    from ontos.extraction.base import ExtractionResult
    from ontos.ontology import load_ontology
    from ontos.pipeline import ErrorPolicy, IngestPipeline

    class _FakeConnector:
        id = "test-conn"
        source_kind = "test"

        async def iter_documents(self):
            from ontos.ingest import SourceDocument

            yield SourceDocument(source_id="doc1", text="hi")

    class _FakeExtractor:
        id = "test-extractor"
        version = "0.0"

        async def extract(self, inp):
            prov = Provenance(
                source_id="doc1",
                extractor_id="test-extractor",
                extractor_version="0.0",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            )
            e_alice = Entity(id="alice", type="Person", canonical_name="Alice", provenance=prov)
            e_acme = Entity(id="acme", type="Company", canonical_name="Acme", provenance=prov)
            f = Fact(
                subject_id="alice",
                predicate="works_at",
                object_id="acme",
                provenance=prov,
                t_valid=datetime(2026, 1, 1),
                ingested_at=datetime(2026, 1, 1),
            )
            return ExtractionResult(entities=[e_alice, e_acme], facts=[f])

    starter = (
        Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
    )
    ontology = load_ontology(starter)

    store = NetworkxStore()
    # Spy on upsert_entity and add_fact call order.
    real_upsert = store.upsert_entity
    real_add_fact = store.add_fact
    call_order: list[str] = []

    async def _spy_upsert(entity: Entity, *, acl_ref: str | None = None) -> None:
        call_order.append(f"upsert:{entity.id}")
        await real_upsert(entity, acl_ref=acl_ref)

    async def _spy_add_fact(fact: Fact) -> None:
        call_order.append(f"add_fact:{fact.subject_id}->{fact.object_id}")
        await real_add_fact(fact)

    store.upsert_entity = _spy_upsert  # type: ignore[method-assign]
    store.add_fact = _spy_add_fact  # type: ignore[method-assign]

    pipeline = IngestPipeline(
        connector=_FakeConnector(),
        extractor=_FakeExtractor(),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    report = await pipeline.run()
    assert report.facts_written == 1

    # Both upserts must come before the add_fact.
    assert call_order == [
        "upsert:alice",
        "upsert:acme",
        "add_fact:alice->acme",
    ]
    _ = AsyncMock  # keep import satisfied if moved around


async def test_upsert_entity_stamps_acl_ref_on_first_write() -> None:
    """#38 review: entity extracted from a restricted doc should get
    the doc's acl_ref so future entity-attribute reads can enforce it."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(), acl_ref="acl:finance")
    assert store._graph.nodes["alice"]["acl_ref"] == "acl:finance"  # noqa: SLF001


async def test_upsert_entity_acl_is_first_write_wins() -> None:
    """A later public ingest must NOT downgrade a previously-stamped ACL.
    Prevents a silent leak of a restricted entity via #32's exporter."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(), acl_ref="acl:finance")
    # Re-upsert with no acl_ref — restrictive stamp must persist.
    await store.upsert_entity(_entity(canonical_name="Alice Updated"), acl_ref=None)
    attrs = store._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["acl_ref"] == "acl:finance"
    # Other attrs still last-write-wins.
    assert attrs["canonical_name"] == "Alice Updated"


async def test_upsert_entity_acl_also_first_write_wins_vs_different_label() -> None:
    """Even a *different* non-null ACL on a later write doesn't overwrite
    the stamped one — operators who need to broaden ACLs use a dedicated
    admin path, not routine ingest."""
    store = NetworkxStore()
    await store.upsert_entity(_entity(), acl_ref="acl:finance")
    await store.upsert_entity(_entity(), acl_ref="acl:hr")
    assert store._graph.nodes["alice"]["acl_ref"] == "acl:finance"  # noqa: SLF001


async def test_upsert_entity_round_trips_through_persistence(tmp_path) -> None:
    """New entity attrs survive the pickle round-trip (no magic bump
    needed since nx serializes node attrs natively)."""
    pkl = tmp_path / "store.pkl"
    writer = NetworkxStore(path=pkl)
    await writer.upsert_entity(_entity(aliases=["A."], properties={"team": "r"}))
    await writer.close()

    reader = NetworkxStore(path=pkl)
    attrs = reader._graph.nodes["alice"]  # noqa: SLF001
    assert attrs["type"] == "Person"
    assert attrs["canonical_name"] == "Alice"
    assert attrs["aliases"] == ["A."]
    assert attrs["properties"] == {"team": "r"}
    assert attrs["provenance_source_id"] == "doc1"
