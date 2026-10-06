"""GraphML exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from ontos.resolver.base import MergeRecord
from ontos.storage.models import GraphScan

_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
ET.register_namespace("", _GRAPHML_NS)


def _q(tag: str) -> str:
    return f"{{{_GRAPHML_NS}}}{tag}"


_NODE_KEYS = {
    "raw_id": "n_raw_id",
    "type": "n_type",
    "canonical_name": "n_canonical_name",
    "aliases": "n_aliases",
    "properties": "n_properties",
    "provenance_source_id": "n_provenance_source_id",
    "provenance_extractor_id": "n_provenance_extractor_id",
    "acl_ref": "n_acl_ref",
}

_EDGE_KEYS = {
    "kind": "e_kind",
    "predicate": "e_predicate",
    "fact_id": "e_fact_id",
    "source_id": "e_source_id",
    "extractor_id": "e_extractor_id",
    "extractor_version": "e_extractor_version",
    "confidence": "e_confidence",
    "confidence_score": "e_confidence_score",
    "t_valid": "e_t_valid",
    "t_invalid": "e_t_invalid",
    "ingested_at": "e_ingested_at",
    "superseded_by": "e_superseded_by",
    "acl_ref": "e_acl_ref",
    "resolver_id": "e_resolver_id",
    "resolver_version": "e_resolver_version",
    "resolved_at": "e_resolved_at",
    "reason": "e_reason",
}


def _declare_keys(root: ET.Element) -> None:
    for name, key_id in _NODE_KEYS.items():
        ET.SubElement(
            root,
            _q("key"),
            {
                "id": key_id,
                "for": "node",
                "attr.name": name,
                "attr.type": "string",
            },
        )
    for name, key_id in _EDGE_KEYS.items():
        ET.SubElement(
            root,
            _q("key"),
            {
                "id": key_id,
                "for": "edge",
                "attr.name": name,
                "attr.type": "string",
            },
        )


def _add_data(parent: ET.Element, key_id: str, value: object | None) -> None:
    if value is None:
        return
    data = ET.SubElement(parent, _q("data"), key=key_id)
    data.text = str(value)


def render_graphml(scan: GraphScan) -> str:
    """Render a deterministic GraphML document from a ``GraphScan``."""

    root = ET.Element(_q("graphml"))
    _declare_keys(root)
    graph = ET.SubElement(root, _q("graph"), id="ontos", edgedefault="directed")

    for entity in sorted(scan.entities, key=lambda item: item.id):
        node = ET.SubElement(graph, _q("node"), id=entity.id)
        _add_data(node, _NODE_KEYS["raw_id"], entity.id)
        _add_data(node, _NODE_KEYS["type"], entity.type)
        _add_data(node, _NODE_KEYS["canonical_name"], entity.canonical_name)
        _add_data(
            node,
            _NODE_KEYS["aliases"],
            json.dumps(entity.aliases, sort_keys=True, separators=(",", ":")),
        )
        _add_data(
            node,
            _NODE_KEYS["properties"],
            json.dumps(entity.properties, sort_keys=True, separators=(",", ":")),
        )
        _add_data(node, _NODE_KEYS["provenance_source_id"], entity.provenance_source_id)
        _add_data(node, _NODE_KEYS["provenance_extractor_id"], entity.provenance_extractor_id)
        _add_data(node, _NODE_KEYS["acl_ref"], entity.acl_ref)

    for fact in sorted(scan.facts, key=lambda item: str(item.id)):
        edge = ET.SubElement(
            graph,
            _q("edge"),
            id=str(fact.id),
            source=fact.subject_id,
            target=fact.object_id,
        )
        _add_data(edge, _EDGE_KEYS["kind"], "fact")
        _add_data(edge, _EDGE_KEYS["predicate"], fact.predicate)
        _add_data(edge, _EDGE_KEYS["fact_id"], fact.id)
        _add_data(edge, _EDGE_KEYS["source_id"], fact.provenance.source_id)
        _add_data(edge, _EDGE_KEYS["extractor_id"], fact.provenance.extractor_id)
        _add_data(edge, _EDGE_KEYS["extractor_version"], fact.provenance.extractor_version)
        _add_data(edge, _EDGE_KEYS["confidence"], fact.provenance.confidence.value)
        _add_data(edge, _EDGE_KEYS["confidence_score"], fact.provenance.confidence_score)
        _add_data(edge, _EDGE_KEYS["t_valid"], fact.t_valid.isoformat())
        _add_data(
            edge,
            _EDGE_KEYS["t_invalid"],
            fact.t_invalid.isoformat() if fact.t_invalid is not None else None,
        )
        _add_data(edge, _EDGE_KEYS["ingested_at"], fact.ingested_at.isoformat())
        _add_data(edge, _EDGE_KEYS["superseded_by"], fact.superseded_by)
        _add_data(edge, _EDGE_KEYS["acl_ref"], fact.acl_ref)

    merge_edges: list[tuple[str, str, str, MergeRecord]] = []
    for record in scan.merges:
        for merged_id in record.merged_ids:
            merge_edges.append((record.canonical_id, merged_id, record.resolver_id, record))

    ordered_merges = sorted(
        merge_edges,
        key=lambda item: (item[0], item[1], item[2], item[3].resolved_at.isoformat()),
    )
    for index, (canonical_id, merged_id, _resolver_id, record) in enumerate(ordered_merges):
        edge = ET.SubElement(
            graph,
            _q("edge"),
            id=f"merge-{index}",
            source=merged_id,
            target=canonical_id,
        )
        _add_data(edge, _EDGE_KEYS["kind"], "merge")
        _add_data(edge, _EDGE_KEYS["predicate"], "MERGED_WITH")
        _add_data(edge, _EDGE_KEYS["resolver_id"], record.resolver_id)
        _add_data(edge, _EDGE_KEYS["resolver_version"], record.resolver_version)
        _add_data(edge, _EDGE_KEYS["resolved_at"], record.resolved_at.isoformat())
        _add_data(edge, _EDGE_KEYS["reason"], record.reason)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)
