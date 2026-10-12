"""GraphML exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.models import GraphScan, StoredEntity

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
    "ontology_id": "e_ontology_id",
    "ontology_version": "e_ontology_version",
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


def _data_values(
    element: ET.Element,
    key_names: dict[str, str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for data in element.findall(_q("data")):
        key_id = data.attrib.get("key")
        if key_id is None or key_id not in key_names:
            continue
        values[key_names[key_id]] = data.text or ""
    return values


def load_graphml(path: Path) -> GraphScan:
    """Load a #32 GraphML snapshot into the backend-neutral graph model."""

    root = ET.fromstring(path.read_text(encoding="utf-8"))
    if root.tag != _q("graphml"):
        raise ValueError("GraphML snapshot root must be <graphml>")

    node_key_names: dict[str, str] = {}
    edge_key_names: dict[str, str] = {}
    for key in root.findall(_q("key")):
        key_id = key.attrib.get("id")
        attr_name = key.attrib.get("attr.name")
        key_kind = key.attrib.get("for")
        if key_id is None or attr_name is None:
            continue
        if key_kind == "node":
            node_key_names[key_id] = attr_name
        elif key_kind == "edge":
            edge_key_names[key_id] = attr_name

    graph = root.find(_q("graph"))
    if graph is None:
        raise ValueError("GraphML snapshot must contain a <graph> element")

    entities: list[StoredEntity] = []
    facts: list[Fact] = []
    merge_groups: dict[
        tuple[str, str, str, datetime, str],
        set[str],
    ] = {}

    for node in graph.findall(_q("node")):
        values = _data_values(node, node_key_names)
        raw_id = values.get("raw_id", node.attrib.get("id"))
        if raw_id is None:
            raise ValueError("GraphML node must contain raw_id or id")

        aliases_raw = json.loads(values.get("aliases", "[]"))
        properties_raw = json.loads(values.get("properties", "{}"))
        if not isinstance(aliases_raw, list):
            raise ValueError("GraphML entity aliases must decode to a list")
        if not isinstance(properties_raw, dict):
            raise ValueError("GraphML entity properties must decode to an object")

        entities.append(
            StoredEntity(
                id=raw_id,
                type=values.get("type"),
                canonical_name=values.get("canonical_name"),
                aliases=[str(alias) for alias in aliases_raw],
                properties={str(key): str(value) for key, value in properties_raw.items()},
                provenance_source_id=values.get("provenance_source_id"),
                provenance_extractor_id=values.get("provenance_extractor_id"),
                acl_ref=values.get("acl_ref"),
            )
        )

    for edge in graph.findall(_q("edge")):
        values = _data_values(edge, edge_key_names)
        kind = values.get("kind")
        source = edge.attrib.get("source")
        target = edge.attrib.get("target")
        if source is None or target is None:
            raise ValueError("GraphML edge must contain source and target")

        if kind == "fact":
            fact_id_raw = values.get("fact_id", edge.attrib.get("id"))
            if fact_id_raw is None:
                raise ValueError("GraphML fact edge must contain fact_id or id")

            superseded_raw = values.get("superseded_by")
            facts.append(
                Fact(
                    id=UUID(fact_id_raw),
                    subject_id=source,
                    predicate=values["predicate"],
                    object_id=target,
                    provenance=Provenance(
                        source_id=values["source_id"],
                        extractor_id=values["extractor_id"],
                        extractor_version=values["extractor_version"],
                        ontology_id=values.get("ontology_id", ""),
                        ontology_version=values.get("ontology_version", ""),
                        confidence=Confidence(values["confidence"]),
                        confidence_score=float(values["confidence_score"]),
                    ),
                    t_valid=datetime.fromisoformat(values["t_valid"]),
                    t_invalid=(
                        datetime.fromisoformat(values["t_invalid"])
                        if values.get("t_invalid")
                        else None
                    ),
                    ingested_at=datetime.fromisoformat(values["ingested_at"]),
                    superseded_by=UUID(superseded_raw) if superseded_raw else None,
                    acl_ref=values.get("acl_ref"),
                )
            )
            continue

        if kind == "merge":
            if values.get("predicate") != "MERGED_WITH":
                raise ValueError("GraphML merge edge must use MERGED_WITH")

            resolved_at = datetime.fromisoformat(values["resolved_at"])
            resolver_id = values["resolver_id"]
            resolver_version = values["resolver_version"]
            reason = values.get("reason", "")

            merge_key = (
                target,
                resolver_id,
                resolver_version,
                resolved_at,
                reason,
            )
            merge_groups.setdefault(merge_key, set()).add(source)
            continue

        raise ValueError(f"Unsupported GraphML edge kind: {kind!r}")

    merges = [
        MergeRecord(
            canonical_id=canonical_id,
            merged_ids=sorted(merged_ids),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            resolved_at=resolved_at,
            reason=reason,
        )
        for (
            canonical_id,
            resolver_id,
            resolver_version,
            resolved_at,
            reason,
        ), merged_ids in merge_groups.items()
    ]

    return GraphScan(
        entities=sorted(entities, key=lambda item: item.id),
        facts=sorted(facts, key=lambda item: str(item.id)),
        merges=sorted(
            merges,
            key=lambda item: (
                item.canonical_id,
                tuple(item.merged_ids),
                item.resolver_id,
                item.resolved_at.isoformat(),
            ),
        ),
    )


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
        _add_data(edge, _EDGE_KEYS["ontology_id"], fact.provenance.ontology_id)
        _add_data(edge, _EDGE_KEYS["ontology_version"], fact.provenance.ontology_version)
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
