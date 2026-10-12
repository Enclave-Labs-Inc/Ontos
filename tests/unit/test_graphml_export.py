"""#32: GraphML exporter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET

from ontos.exporters.graphml import load_graphml, render_graphml
from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.models import GraphScan, StoredEntity

_GRAPHML_NS = {"g": "http://graphml.graphdrawing.org/xmlns"}


def _fact() -> Fact:
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    return Fact(
        id=UUID("00000000-0000-0000-0000-000000000123"),
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme-1",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1.2.3",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.97,
        ),
        t_valid=when,
        ingested_at=when,
        acl_ref="acl:finance",
    )


def _scan() -> GraphScan:
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
        facts=[_fact()],
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


def _data_map(element: ET.Element, root: ET.Element, kind: str) -> dict[str, str]:
    keys = {
        key.attrib["id"]: key.attrib["attr.name"]
        for key in root.findall("g:key", _GRAPHML_NS)
        if key.attrib["for"] == kind
    }
    return {
        keys[data.attrib["key"]]: data.text or "" for data in element.findall("g:data", _GRAPHML_NS)
    }


def test_render_graphml_preserves_raw_nodes_and_fact_metadata() -> None:
    xml = render_graphml(_scan())
    root = ET.fromstring(xml)
    graph = root.find("g:graph", _GRAPHML_NS)
    assert graph is not None

    nodes = {node.attrib["id"]: node for node in graph.findall("g:node", _GRAPHML_NS)}
    assert set(nodes) == {"person:alice", "company:acme-1", "company:acme-2"}

    alice = _data_map(nodes["person:alice"], root, "node")
    assert alice["raw_id"] == "person:alice"
    assert alice["type"] == "Person"
    assert alice["canonical_name"] == "Alice"
    assert alice["properties"] == '{"team":"finance"}'
    assert alice["acl_ref"] == "acl:finance"

    fact_edge = next(
        edge
        for edge in graph.findall("g:edge", _GRAPHML_NS)
        if edge.attrib["id"] == "00000000-0000-0000-0000-000000000123"
    )
    assert fact_edge.attrib["source"] == "person:alice"
    assert fact_edge.attrib["target"] == "company:acme-1"

    fact_data = _data_map(fact_edge, root, "edge")
    assert fact_data["kind"] == "fact"
    assert fact_data["predicate"] == "works_at"
    assert fact_data["source_id"] == "doc:1"
    assert fact_data["ontology_id"] == "ontos.starter"
    assert fact_data["ontology_version"] == "0.4"
    assert fact_data["confidence"] == "EXTRACTED"
    assert fact_data["confidence_score"] == "0.97"
    assert fact_data["acl_ref"] == "acl:finance"


def test_render_graphml_emits_merge_record_as_merged_with_edge() -> None:
    root = ET.fromstring(render_graphml(_scan()))
    graph = root.find("g:graph", _GRAPHML_NS)
    assert graph is not None

    merge_edge = next(
        edge
        for edge in graph.findall("g:edge", _GRAPHML_NS)
        if _data_map(edge, root, "edge").get("kind") == "merge"
    )

    assert merge_edge.attrib["source"] == "company:acme-2"
    assert merge_edge.attrib["target"] == "company:acme-1"

    merge_data = _data_map(merge_edge, root, "edge")
    assert merge_data["predicate"] == "MERGED_WITH"
    assert merge_data["resolver_id"] == "exact-match"
    assert merge_data["resolver_version"] == "1.0"
    assert merge_data["reason"] == "same registration number"


def test_render_graphml_is_deterministic_for_input_order() -> None:
    scan = _scan()
    reversed_scan = scan.model_copy(
        update={
            "entities": list(reversed(scan.entities)),
            "facts": list(reversed(scan.facts)),
            "merges": list(reversed(scan.merges)),
        }
    )

    assert render_graphml(scan) == render_graphml(reversed_scan)


def test_load_graphml_round_trips_exported_snapshot(tmp_path: Path) -> None:
    original = _scan()
    snapshot = tmp_path / "snapshot.graphml"
    snapshot.write_text(render_graphml(original), encoding="utf-8")

    loaded = load_graphml(snapshot)

    assert render_graphml(loaded) == render_graphml(original)
