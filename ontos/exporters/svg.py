"""Deterministic, dependency-free SVG renderer for graph snapshots.

Entities are laid out in one column per entity type, ordered so that
relationships mostly flow left to right (types that are mostly subjects
first). Facts are curved, arrowed edges labelled with their predicate and
source document; parallel facts fan out instead of overlapping, and edges
between entities of the same type arc around the column instead of cutting
through the boxes between them. Every element keeps its `data-*` metadata,
and hovering shows provenance.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Fact
from ontos.storage.models import GraphScan, StoredEntity

_NODE_WIDTH = 210
_NODE_HEIGHT = 58
_COLUMN_GAP = 260
_ROW_GAP = 46
_LEFT = 40
_TOP = 110
_ARC_REACH = 90
# Separation between parallel edges; labels are two lines (~28px) tall.
_FAN = 48

# (fill, stroke) per entity type, assigned in sorted type order.
_PALETTE = [
    ("#DBEAFE", "#2563EB"),
    ("#DCFCE7", "#16A34A"),
    ("#FEF3C7", "#D97706"),
    ("#F3E8FF", "#9333EA"),
    ("#FFE4E6", "#E11D48"),
    ("#E0F2FE", "#0284C7"),
]
_UNTYPED = ("#F1F5F9", "#64748B")
_EDGE = "#475569"
_MERGE = "#9333EA"
_FONT = "Inter, -apple-system, Segoe UI, Helvetica, Arial, sans-serif"


def _entity_type(entity: StoredEntity) -> str:
    return entity.type or "Entity"


def _entity_label(entity: StoredEntity) -> str:
    return entity.canonical_name or entity.id


def _type_colors(entities: Iterable[StoredEntity]) -> dict[str, tuple[str, str]]:
    types = sorted({entity.type for entity in entities if entity.type})
    colors = {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(types)}
    colors["Entity"] = _UNTYPED
    return colors


def _column_order(entities: list[StoredEntity], facts: list[Fact]) -> list[str]:
    """Types that are mostly subjects come first, so edges flow left to right."""
    type_of = {entity.id: _entity_type(entity) for entity in entities}
    flow: dict[str, int] = defaultdict(int)
    for fact in facts:
        subject, target = type_of.get(fact.subject_id), type_of.get(fact.object_id)
        if subject and target and subject != target:
            flow[subject] += 1
            flow[target] -= 1
    return sorted({_entity_type(e) for e in entities}, key=lambda t: (-flow[t], t))


def _layout(
    entities: list[StoredEntity], facts: list[Fact]
) -> tuple[dict[str, tuple[int, int]], dict[str, int], int, int]:
    """Positions by entity id, column index by id, canvas width and height."""
    columns = _column_order(entities, facts)
    members: dict[str, list[StoredEntity]] = defaultdict(list)
    for entity in entities:
        members[_entity_type(entity)].append(entity)

    tallest = max((len(group) for group in members.values()), default=1)
    column_height = tallest * _NODE_HEIGHT + (tallest - 1) * _ROW_GAP
    positions: dict[str, tuple[int, int]] = {}
    column_of: dict[str, int] = {}
    for index, entity_type in enumerate(columns):
        group = sorted(members[entity_type], key=lambda e: (_entity_label(e).casefold(), e.id))
        height = len(group) * _NODE_HEIGHT + (len(group) - 1) * _ROW_GAP
        top = _TOP + (column_height - height) // 2
        x = _LEFT + index * (_NODE_WIDTH + _COLUMN_GAP)
        for row, entity in enumerate(group):
            positions[entity.id] = (x, top + row * (_NODE_HEIGHT + _ROW_GAP))
            column_of[entity.id] = index

    width = _LEFT * 2 + len(columns) * _NODE_WIDTH + max(len(columns) - 1, 0) * _COLUMN_GAP
    width += _ARC_REACH + _FAN * 2 + 90  # room for arcs and their labels
    height = _TOP + column_height + 60
    return positions, column_of, max(width, 520), max(height, 220)


def _defs(root: ET.Element) -> None:
    defs = ET.SubElement(root, "defs")
    for marker_id, color in (("arrow", _EDGE), ("arrow-merge", _MERGE)):
        marker = ET.SubElement(
            defs,
            "marker",
            {
                "id": marker_id,
                "viewBox": "0 0 10 10",
                "refX": "9",
                "refY": "5",
                "markerWidth": "7",
                "markerHeight": "7",
                "orient": "auto-start-reverse",
            },
        )
        ET.SubElement(marker, "path", {"d": "M 0 0 L 10 5 L 0 10 z", "fill": color})
    shadow = ET.SubElement(
        defs,
        "filter",
        {"id": "shadow", "x": "-10%", "y": "-10%", "width": "130%", "height": "140%"},
    )
    ET.SubElement(
        shadow,
        "feDropShadow",
        {
            "dx": "0",
            "dy": "1.5",
            "stdDeviation": "2",
            "flood-color": "#0f172a",
            "flood-opacity": "0.12",
        },
    )


def _header(
    root: ET.Element,
    entities: list[StoredEntity],
    facts: list[Fact],
    colors: dict[str, tuple[str, str]],
) -> None:
    title = ET.SubElement(
        root,
        "text",
        {
            "x": str(_LEFT),
            "y": "38",
            "font-family": _FONT,
            "font-size": "20",
            "font-weight": "600",
            "fill": "#0f172a",
        },
    )
    title.text = "Knowledge graph"
    sources = len({fact.provenance.source_id for fact in facts})
    summary = ET.SubElement(
        root,
        "text",
        {"x": str(_LEFT), "y": "60", "font-family": _FONT, "font-size": "13", "fill": "#64748b"},
    )
    summary.text = (
        f"{len(entities)} entities · {len(facts)} facts · {sources} source document"
        f"{'' if sources == 1 else 's'} · every fact keeps its provenance"
    )
    x = _LEFT
    for entity_type in sorted({_entity_type(e) for e in entities}):
        fill, stroke = colors.get(entity_type, _UNTYPED)
        ET.SubElement(
            root,
            "rect",
            {
                "x": str(x),
                "y": "74",
                "width": "12",
                "height": "12",
                "rx": "3",
                "fill": fill,
                "stroke": stroke,
            },
        )
        chip = ET.SubElement(
            root,
            "text",
            {
                "x": str(x + 18),
                "y": "84",
                "font-family": _FONT,
                "font-size": "12",
                "fill": "#334155",
            },
        )
        chip.text = entity_type
        x += 30 + 8 * len(entity_type)


def _add_entity(
    root: ET.Element,
    entity: StoredEntity,
    *,
    x: int,
    y: int,
    colors: dict[str, tuple[str, str]],
) -> None:
    attrs = {"class": "entity", "data-id": entity.id, "data-type": _entity_type(entity)}
    if entity.acl_ref is not None:
        attrs["data-acl-ref"] = entity.acl_ref
    group = ET.SubElement(root, "g", attrs)
    ET.SubElement(group, "title").text = f"{_entity_label(entity)}\nid: {entity.id}"

    fill, stroke = colors.get(_entity_type(entity), _UNTYPED)
    ET.SubElement(
        group,
        "rect",
        {
            "x": str(x),
            "y": str(y),
            "width": str(_NODE_WIDTH),
            "height": str(_NODE_HEIGHT),
            "rx": "12",
            "fill": fill,
            "stroke": stroke,
            "stroke-width": "1.5",
            "filter": "url(#shadow)",
        },
    )
    ET.SubElement(
        group,
        "rect",
        {
            "x": str(x),
            "y": str(y),
            "width": "6",
            "height": str(_NODE_HEIGHT),
            "rx": "3",
            "fill": stroke,
        },
    )
    label = ET.SubElement(
        group,
        "text",
        {
            "x": str(x + 18),
            "y": str(y + 26),
            "font-family": _FONT,
            "font-size": "15",
            "font-weight": "600",
            "fill": "#0f172a",
        },
    )
    label.text = _entity_label(entity)
    kind = ET.SubElement(
        group,
        "text",
        {
            "x": str(x + 18),
            "y": str(y + 45),
            "font-family": _FONT,
            "font-size": "11",
            "fill": stroke,
        },
    )
    kind.text = _entity_type(entity)


def _curve(
    subject: tuple[int, int],
    target: tuple[int, int],
    *,
    same_column: bool,
    lane: int,
) -> tuple[str, tuple[float, float]]:
    """SVG path data and the label anchor for one edge."""
    c1: tuple[float, float]
    c2: tuple[float, float]
    if same_column:
        # Arc out to the right of the column, never through other boxes.
        sx, sy = subject[0] + _NODE_WIDTH, subject[1] + _NODE_HEIGHT / 2
        tx, ty = target[0] + _NODE_WIDTH, target[1] + _NODE_HEIGHT / 2
        # Lanes alternate 0, 1, -1, 2, -2 ...; arcs only fan outward, so
        # map them to a strictly increasing reach (lane -2 at 90 - 96 would
        # otherwise fold back through the column).
        reach = _ARC_REACH + (2 * abs(lane) - (lane > 0)) * _FAN / 2
        c1, c2 = (sx + reach, sy), (tx + reach, ty)
    else:
        forward = target[0] > subject[0]
        sx = subject[0] + (_NODE_WIDTH if forward else 0)
        tx = target[0] + (0 if forward else _NODE_WIDTH)
        sy, ty = subject[1] + _NODE_HEIGHT / 2, target[1] + _NODE_HEIGHT / 2
        bend = (tx - sx) * 0.45
        offset = lane * _FAN
        c1, c2 = (sx + bend, sy + offset), (tx - bend, ty + offset)
    # Arcs are labelled at their outermost point; other edges a little
    # toward the source, so labels of edges converging on one node sit
    # beside their own source row instead of piling up mid-way.
    t = 0.5 if same_column else 0.4
    u = 1 - t
    mid = (
        u**3 * sx + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * tx,
        u**3 * sy + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * ty,
    )
    data = (
        f"M {sx:.1f} {sy:.1f} C {c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {tx:.1f} {ty:.1f}"
    )
    return data, mid


def _add_edge(
    root: ET.Element,
    *,
    path: str,
    anchor: tuple[float, float],
    label: str,
    caption: str,
    tooltip: str,
    attrs: dict[str, str],
    merge: bool = False,
) -> None:
    group = ET.SubElement(root, "g", attrs)
    ET.SubElement(group, "title").text = tooltip
    color = _MERGE if merge else _EDGE
    line = {
        "d": path,
        "fill": "none",
        "stroke": color,
        "stroke-width": "1.6",
        "marker-end": f"url(#{'arrow-merge' if merge else 'arrow'})",
    }
    if merge:
        line["stroke-dasharray"] = "5 4"
    ET.SubElement(group, "path", line)

    x, y = anchor
    halo = {
        "stroke": "white",
        "stroke-width": "4",
        "paint-order": "stroke",
        "text-anchor": "middle",
    }
    text = ET.SubElement(
        group,
        "text",
        {
            "x": f"{x:.1f}",
            "y": f"{y - 3:.1f}",
            "font-family": _FONT,
            "font-size": "12",
            "font-weight": "600",
            "fill": color,
            **halo,
        },
    )
    text.text = label
    if caption:
        small = ET.SubElement(
            group,
            "text",
            {
                "x": f"{x:.1f}",
                "y": f"{y + 11:.1f}",
                "font-family": _FONT,
                "font-size": "10",
                "fill": "#64748b",
                **halo,
            },
        )
        small.text = caption


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


def _merge_edges(records: Iterable[MergeRecord]) -> list[tuple[str, str, MergeRecord]]:
    edges = [
        (merged_id, record.canonical_id, record)
        for record in records
        for merged_id in record.merged_ids
        if merged_id != record.canonical_id
    ]
    return sorted(
        edges,
        key=lambda item: (item[0], item[1], item[2].resolver_id, item[2].resolved_at.isoformat()),
    )


def render_svg(scan: GraphScan) -> str:
    """Render a small dependency-free SVG representation of a graph snapshot."""
    entities = sorted(scan.entities, key=lambda item: item.id)
    facts = sorted(
        scan.facts,
        key=lambda f: (f.subject_id, f.object_id, f.predicate, f.provenance.source_id, str(f.id)),
    )
    colors = _type_colors(entities)
    positions, column_of, width, height = _layout(entities, facts)

    root = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": str(width),
            "height": str(height),
            "viewBox": f"0 0 {width} {height}",
        },
    )
    _defs(root)
    ET.SubElement(root, "rect", {"width": "100%", "height": "100%", "fill": "#f8fafc"})
    _header(root, entities, facts, colors)

    # Parallel edges between one pair of nodes get their own lanes.
    lanes: dict[tuple[str, str], int] = defaultdict(int)

    def lane_for(a: str, b: str) -> int:
        key = (min(a, b), max(a, b))
        index = lanes[key]
        lanes[key] += 1
        # 0, 1, -1, 2, -2 ... so a single edge stays centred.
        return (index + 1) // 2 * (1 if index % 2 else -1)

    for fact in facts:
        subject, target = positions.get(fact.subject_id), positions.get(fact.object_id)
        if subject is None or target is None:
            continue
        path, anchor = _curve(
            subject,
            target,
            same_column=column_of[fact.subject_id] == column_of[fact.object_id],
            lane=lane_for(fact.subject_id, fact.object_id),
        )
        score = fact.provenance.confidence_score
        _add_edge(
            root,
            path=path,
            anchor=anchor,
            label=fact.predicate,
            caption=fact.provenance.source_id,
            tooltip=(
                f"{fact.subject_id} -{fact.predicate}-> {fact.object_id}\n"
                f"source: {fact.provenance.source_id}\n"
                f"extractor: {fact.provenance.extractor_id}"
                + (f"\nconfidence: {score}" if score is not None else "")
            ),
            attrs=_fact_attrs(fact),
        )

    for merged_id, canonical_id, record in _merge_edges(scan.merges):
        subject, target = positions.get(merged_id), positions.get(canonical_id)
        if subject is None or target is None:
            continue
        path, anchor = _curve(
            subject,
            target,
            same_column=column_of[merged_id] == column_of[canonical_id],
            lane=lane_for(merged_id, canonical_id),
        )
        _add_edge(
            root,
            path=path,
            anchor=anchor,
            label="MERGED_WITH",
            caption=record.resolver_id,
            tooltip=f"{merged_id} merged into {canonical_id}\n{record.reason}",
            attrs={
                "class": "edge",
                "data-kind": "merge",
                "data-predicate": "MERGED_WITH",
                "data-resolver-id": record.resolver_id,
                "data-resolver-version": record.resolver_version,
                "data-resolved-at": record.resolved_at.isoformat(),
                "data-reason": record.reason,
            },
            merge=True,
        )

    for entity in entities:
        x, y = positions[entity.id]
        _add_entity(root, entity, x=x, y=y, colors=colors)

    return ET.tostring(root, encoding="unicode")
