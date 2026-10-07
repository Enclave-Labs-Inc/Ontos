"""Deterministic SVG fallback renderer for graph snapshots."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Fact
from ontos.storage.models import GraphScan, StoredEntity

_NODE_WIDTH = 220
_NODE_HEIGHT = 56
_LEFT = 40
_TOP = 40
_VERTICAL_GAP = 100


def _entity_label(entity: StoredEntity) -> str:
    return entity.canonical_name or entity.id


def _entity_positions(
    entities: Iterable[StoredEntity],
) -> dict[str, tuple[int, int]]:
    positions: dict[str, tuple[int, int]] = {}

    for index, entity in enumerate(sorted(entities, key=lambda item: item.id)):
        positions[entity.id] = (
            _LEFT,
            _TOP + index * (_NODE_HEIGHT + _VERTICAL_GAP),
        )

    return positions


def _add_entity(
    root: ET.Element,
    entity: StoredEntity,
    *,
    x: int,
    y: int,
) -> None:
    attrs = {
        "class": "entity",
        "data-id": entity.id,
        "data-type": entity.type or "Entity",
    }
    if entity.acl_ref is not None:
        attrs["data-acl-ref"] = entity.acl_ref

    group = ET.SubElement(root, "g", attrs)

    ET.SubElement(
        group,
        "rect",
        {
            "x": str(x),
            "y": str(y),
            "width": str(_NODE_WIDTH),
            "height": str(_NODE_HEIGHT),
            "rx": "8",
            "fill": "white",
            "stroke": "black",
        },
    )

    label = ET.SubElement(
        group,
        "text",
        {
            "x": str(x + 12),
            "y": str(y + 24),
            "font-family": "sans-serif",
            "font-size": "14",
        },
    )
    label.text = _entity_label(entity)

    raw_id = ET.SubElement(
        group,
        "text",
        {
            "x": str(x + 12),
            "y": str(y + 43),
            "font-family": "monospace",
            "font-size": "10",
        },
    )
    raw_id.text = entity.id


def _add_edge(
    root: ET.Element,
    *,
    subject_id: str,
    object_id: str,
    predicate: str,
    positions: dict[str, tuple[int, int]],
    attrs: dict[str, str],
) -> None:
    subject = positions.get(subject_id)
    target = positions.get(object_id)
    if subject is None or target is None:
        return

    sx = subject[0] + _NODE_WIDTH
    sy = subject[1] + _NODE_HEIGHT // 2
    tx = target[0]
    ty = target[1] + _NODE_HEIGHT // 2

    group = ET.SubElement(root, "g", attrs)

    ET.SubElement(
        group,
        "line",
        {
            "x1": str(sx),
            "y1": str(sy),
            "x2": str(tx),
            "y2": str(ty),
            "stroke": "black",
        },
    )

    label = ET.SubElement(
        group,
        "text",
        {
            "x": str((sx + tx) // 2),
            "y": str((sy + ty) // 2 - 6),
            "font-family": "sans-serif",
            "font-size": "11",
        },
    )
    label.text = predicate


def _fact_attrs(fact: Fact) -> dict[str, str]:
    attrs = {
        "class": "edge",
        "data-kind": "fact",
        "data-fact-id": str(fact.id),
        "data-predicate": fact.predicate,
        "data-source-id": fact.provenance.source_id,
        "data-extractor-id": fact.provenance.extractor_id,
        "data-extractor-version": fact.provenance.extractor_version,
        "data-ontology-id": fact.provenance.ontology_id,
        "data-ontology-version": fact.provenance.ontology_version,
        "data-confidence": fact.provenance.confidence.value,
        "data-t-valid": fact.t_valid.isoformat(),
        "data-ingested-at": fact.ingested_at.isoformat(),
    }

    if fact.provenance.confidence_score is not None:
        attrs["data-confidence-score"] = str(fact.provenance.confidence_score)
    if fact.t_invalid is not None:
        attrs["data-t-invalid"] = fact.t_invalid.isoformat()
    if fact.superseded_by is not None:
        attrs["data-superseded-by"] = str(fact.superseded_by)
    if fact.acl_ref is not None:
        attrs["data-acl-ref"] = fact.acl_ref

    return attrs


def _merge_edges(
    records: Iterable[MergeRecord],
) -> list[tuple[str, str, MergeRecord]]:
    edges: list[tuple[str, str, MergeRecord]] = []

    for record in records:
        for merged_id in record.merged_ids:
            edges.append((merged_id, record.canonical_id, record))

    return sorted(
        edges,
        key=lambda item: (
            item[0],
            item[1],
            item[2].resolver_id,
            item[2].resolved_at.isoformat(),
        ),
    )


def render_svg(scan: GraphScan) -> str:
    """Render a small dependency-free SVG representation of a graph snapshot."""

    entities = sorted(scan.entities, key=lambda item: item.id)
    positions = _entity_positions(entities)

    height = max(
        160,
        _TOP + len(entities) * (_NODE_HEIGHT + _VERTICAL_GAP),
    )
    width = 600

    root = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": str(width),
            "height": str(height),
            "viewBox": f"0 0 {width} {height}",
        },
    )

    for fact in sorted(scan.facts, key=lambda item: str(item.id)):
        _add_edge(
            root,
            subject_id=fact.subject_id,
            object_id=fact.object_id,
            predicate=fact.predicate,
            positions=positions,
            attrs=_fact_attrs(fact),
        )

    for merged_id, canonical_id, record in _merge_edges(scan.merges):
        attrs = {
            "class": "edge",
            "data-kind": "merge",
            "data-predicate": "MERGED_WITH",
            "data-resolver-id": record.resolver_id,
            "data-resolver-version": record.resolver_version,
            "data-resolved-at": record.resolved_at.isoformat(),
            "data-reason": record.reason,
        }
        _add_edge(
            root,
            subject_id=merged_id,
            object_id=canonical_id,
            predicate="MERGED_WITH",
            positions=positions,
            attrs=attrs,
        )

    for entity in entities:
        x, y = positions[entity.id]
        _add_entity(root, entity, x=x, y=y)

    return ET.tostring(root, encoding="unicode")
