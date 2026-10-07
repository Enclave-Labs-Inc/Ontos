"""#32: NetworkxStore graph snapshot export."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _provenance(source_id: str = "doc:1") -> Provenance:
    return Provenance(
        source_id=source_id,
        extractor_id="extractor:test",
        extractor_version="1.2.3",
        ontology_id="ontos.starter",
        ontology_version="0.4",
        confidence=Confidence.EXTRACTED,
        confidence_score=0.97,
    )


def _entity(
    entity_id: str,
    *,
    entity_type: str,
    canonical_name: str,
    aliases: list[str] | None = None,
    properties: dict[str, str] | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        type=entity_type,
        canonical_name=canonical_name,
        aliases=aliases or [],
        properties=properties or {},
        provenance=_provenance(),
    )


def _fact(
    subject_id: str,
    predicate: str,
    object_id: str,
    *,
    fact_id: str,
    acl_ref: str | None = None,
) -> Fact:
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    return Fact(
        id=UUID(fact_id),
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        provenance=_provenance(),
        t_valid=when,
        ingested_at=when,
        acl_ref=acl_ref,
    )


async def test_scan_preserves_raw_ids_and_stored_entity_attributes() -> None:
    store = NetworkxStore()

    await store.upsert_entity(
        _entity(
            "person:alice",
            entity_type="Person",
            canonical_name="Alice",
            aliases=["A. Smith"],
            properties={"team": "finance"},
        )
    )
    await store.upsert_entity(
        _entity(
            "company:acme-1",
            entity_type="Company",
            canonical_name="Acme",
        )
    )
    await store.add_fact(
        _fact(
            "person:alice",
            "works_at",
            "company:acme-1",
            fact_id="00000000-0000-0000-0000-000000000123",
        )
    )

    scan = await store.scan()

    entities = {entity.id: entity for entity in scan.entities}
    assert set(entities) == {"person:alice", "company:acme-1"}
    assert entities["person:alice"].type == "Person"
    assert entities["person:alice"].canonical_name == "Alice"
    assert entities["person:alice"].aliases == ["A. Smith"]
    assert entities["person:alice"].properties == {"team": "finance"}
    assert entities["person:alice"].provenance_source_id == "doc:1"
    assert entities["person:alice"].provenance_extractor_id == "extractor:test"

    assert len(scan.facts) == 1
    assert scan.facts[0].subject_id == "person:alice"
    assert scan.facts[0].object_id == "company:acme-1"


async def test_scan_temporal_modes() -> None:
    store = NetworkxStore()
    fact = _fact(
        "person:alice",
        "works_at",
        "company:acme-1",
        fact_id="00000000-0000-0000-0000-000000000124",
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


async def test_scan_denies_restricted_entities_and_endpoint_facts_by_default() -> None:
    store = NetworkxStore()

    await store.upsert_entity(
        _entity(
            "person:alice",
            entity_type="Person",
            canonical_name="Alice Finance",
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        _entity(
            "company:public",
            entity_type="Company",
            canonical_name="Public Co",
        )
    )

    # Deliberately public fact with a restricted endpoint: scan must still
    # drop the fact or the raw endpoint id would reveal restricted existence.
    await store.add_fact(
        _fact(
            "person:alice",
            "works_at",
            "company:public",
            fact_id="00000000-0000-0000-0000-000000000125",
        )
    )

    scan = await store.scan()

    assert {entity.id for entity in scan.entities} == {"company:public"}
    assert scan.facts == []


async def test_scan_allows_restricted_graph_when_acl_is_authorized() -> None:
    store = NetworkxStore()

    await store.upsert_entity(
        _entity(
            "person:alice",
            entity_type="Person",
            canonical_name="Alice Finance",
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        _entity(
            "company:acme-1",
            entity_type="Company",
            canonical_name="Acme",
        ),
        acl_ref="acl:finance",
    )
    fact = _fact(
        "person:alice",
        "works_at",
        "company:acme-1",
        fact_id="00000000-0000-0000-0000-000000000126",
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


async def test_scan_filters_merge_records_and_keeps_raw_ids() -> None:
    store = NetworkxStore()

    await store.upsert_entity(
        _entity(
            "company:acme-1",
            entity_type="Company",
            canonical_name="Acme",
        )
    )
    await store.upsert_entity(
        _entity(
            "company:acme-2",
            entity_type="Company",
            canonical_name="Acme Holdings",
        ),
        acl_ref="acl:finance",
    )
    await store.upsert_entity(
        _entity(
            "person:alice",
            entity_type="Person",
            canonical_name="Alice",
        )
    )

    fact = _fact(
        "person:alice",
        "works_at",
        "company:acme-2",
        fact_id="00000000-0000-0000-0000-000000000127",
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

    # Resolver output is separate audit metadata; fact endpoints remain raw.
    assert allowed.facts[0].object_id == "company:acme-2"
