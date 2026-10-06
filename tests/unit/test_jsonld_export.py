"""#32: JSON-LD exporter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from ontos.exporters.jsonld import render_jsonld

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.models import GraphScan, StoredEntity


def _scan() -> GraphScan:
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    fact = Fact(
        id=UUID("00000000-0000-0000-0000-000000000123"),
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme-1",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1.2.3",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.97,
        ),
        t_valid=when,
        ingested_at=when,
        acl_ref="acl:finance",
    )
    return GraphScan(
        entities=[
            StoredEntity(
                id="person:alice",
                type="Person",
                canonical_name="Alice",
                aliases=["A. Smith"],
                properties={"team": "finance"},
                provenance_source_id="doc:1",
                provenance_extractor_id="extractor:test",
                acl_ref="acl:finance",
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


def _graph_by_id(document: dict[str, object]) -> dict[str, dict[str, object]]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    return {
        item["@id"]: item
        for item in graph
        if isinstance(item, dict) and isinstance(item.get("@id"), str)
    }


def test_render_jsonld_preserves_raw_entity_ids_and_attrs() -> None:
    document = json.loads(render_jsonld(_scan()))
    nodes = _graph_by_id(document)

    alice = nodes["person:alice"]
    assert alice["@type"] == "Person"
    assert alice["rawId"] == "person:alice"
    assert alice["canonicalName"] == "Alice"
    assert alice["aliases"] == ["A. Smith"]
    assert alice["properties"] == {"team": "finance"}
    assert alice["aclRef"] == "acl:finance"


def test_render_jsonld_exports_fact_with_provenance_and_time() -> None:
    document = json.loads(render_jsonld(_scan()))
    nodes = _graph_by_id(document)

    fact = nodes["urn:ontos:fact:00000000-0000-0000-0000-000000000123"]
    assert fact["@type"] == "Fact"
    assert fact["subject"] == {"@id": "person:alice"}
    assert fact["predicate"] == "works_at"
    assert fact["object"] == {"@id": "company:acme-1"}
    assert fact["sourceId"] == "doc:1"
    assert fact["extractorId"] == "extractor:test"
    assert fact["confidence"] == "EXTRACTED"
    assert fact["confidenceScore"] == 0.97
    assert fact["tValid"] == "2026-01-02T03:04:00+00:00"
    assert fact["aclRef"] == "acl:finance"


def test_render_jsonld_exports_merge_as_merged_with_statement() -> None:
    document = json.loads(render_jsonld(_scan()))
    nodes = _graph_by_id(document)

    merge = next(
        item
        for item in nodes.values()
        if item.get("@type") == "Merge" and item.get("predicate") == "MERGED_WITH"
    )
    assert merge["subject"] == {"@id": "company:acme-2"}
    assert merge["object"] == {"@id": "company:acme-1"}
    assert merge["resolverId"] == "exact-match"
    assert merge["resolverVersion"] == "1.0"
    assert merge["reason"] == "same registration number"


def test_render_jsonld_is_deterministic() -> None:
    scan = _scan()
    reversed_scan = scan.model_copy(
        update={
            "entities": list(reversed(scan.entities)),
            "facts": list(reversed(scan.facts)),
            "merges": list(reversed(scan.merges)),
        }
    )

    assert render_jsonld(scan) == render_jsonld(reversed_scan)
