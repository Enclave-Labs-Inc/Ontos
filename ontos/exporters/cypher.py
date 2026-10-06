"""Cypher exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import json
import re

from ontos.resolver.base import MergeRecord
from ontos.storage.models import GraphScan, StoredEntity

_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_identifier(
    value: str | None,
    *,
    fallback: str,
    uppercase: bool = False,
) -> str:
    """Return a safe Cypher label or relationship type."""

    cleaned = _IDENTIFIER_RE.sub("", value or "")
    if uppercase:
        cleaned = cleaned.upper()
    return cleaned or fallback


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


def _value(value: object | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return _quote(str(value))


def _props(items: list[tuple[str, object | None]]) -> str:
    rendered = [f"{key}: {_value(value)}" for key, value in items if value is not None]
    return "{" + ", ".join(rendered) + "}"


def _entity_label(entity: StoredEntity) -> str:
    return sanitize_identifier(entity.type, fallback="Entity")


def _relationship_type(predicate: str) -> str:
    return sanitize_identifier(predicate, fallback="RELATED_TO", uppercase=True)


def _node_properties(entity: StoredEntity) -> str:
    return _props(
        [
            ("canonical_name", entity.canonical_name),
            ("aliases", entity.aliases),
            (
                "properties",
                json.dumps(entity.properties, sort_keys=True, separators=(",", ":")),
            ),
            ("provenance_source_id", entity.provenance_source_id),
            ("provenance_extractor_id", entity.provenance_extractor_id),
            ("acl_ref", entity.acl_ref),
        ]
    )


def _merge_edges(scan: GraphScan) -> list[tuple[str, str, str, MergeRecord]]:
    edges: list[tuple[str, str, str, MergeRecord]] = []
    for record in scan.merges:
        for merged_id in record.merged_ids:
            edges.append((record.canonical_id, merged_id, record.resolver_id, record))
    return sorted(
        edges,
        key=lambda item: (item[0], item[1], item[2], item[3].resolved_at.isoformat()),
    )


def render_cypher(scan: GraphScan) -> str:
    """Render deterministic, idempotent Cypher for a visualization database."""

    entities = sorted(scan.entities, key=lambda item: item.id)
    labels_by_id = {entity.id: _entity_label(entity) for entity in entities}
    labels = sorted(set(labels_by_id.values()))

    lines = [
        "// Ontos visualization export.",
        "// Import into a separate visualization database; do not target the operational store.",
        "",
    ]

    for label in labels:
        lines.append(f"CREATE INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n.id);")
    if labels:
        lines.append("")

    for entity in entities:
        label = labels_by_id[entity.id]
        lines.append(
            f"MERGE (n:`{label}` {{id: {_quote(entity.id)}}}) "
            f"SET n += {_node_properties(entity)};"
        )

    if entities and (scan.facts or scan.merges):
        lines.append("")

    for fact in sorted(scan.facts, key=lambda item: str(item.id)):
        subject_label = labels_by_id.get(fact.subject_id, "Entity")
        object_label = labels_by_id.get(fact.object_id, "Entity")
        rel_type = _relationship_type(fact.predicate)
        lines.append(
            f"MATCH (s:`{subject_label}` {{id: {_quote(fact.subject_id)}}}), "
            f"(o:`{object_label}` {{id: {_quote(fact.object_id)}}})"
        )
        lines.append(
            f"MERGE (s)-[r:`{rel_type}` {{fact_id: {_quote(str(fact.id))}}}]->(o)"
        )
        lines.append(
            "SET r += "
            + _props(
                [
                    ("predicate", fact.predicate),
                    ("source_id", fact.provenance.source_id),
                    ("extractor_id", fact.provenance.extractor_id),
                    ("extractor_version", fact.provenance.extractor_version),
                    ("confidence", fact.provenance.confidence.value),
                    ("confidence_score", fact.provenance.confidence_score),
                    ("t_valid", fact.t_valid.isoformat()),
                    (
                        "t_invalid",
                        fact.t_invalid.isoformat() if fact.t_invalid is not None else None,
                    ),
                    ("ingested_at", fact.ingested_at.isoformat()),
                    (
                        "superseded_by",
                        str(fact.superseded_by) if fact.superseded_by is not None else None,
                    ),
                    ("acl_ref", fact.acl_ref),
                ]
            )
            + ";"
        )

    for canonical_id, merged_id, _resolver_id, record in _merge_edges(scan):
        canonical_label = labels_by_id.get(canonical_id, "Entity")
        merged_label = labels_by_id.get(merged_id, "Entity")
        lines.append(
            f"MATCH (m:`{merged_label}` {{id: {_quote(merged_id)}}}), "
            f"(c:`{canonical_label}` {{id: {_quote(canonical_id)}}})"
        )
        lines.append(
            "MERGE (m)-[r:MERGED_WITH "
            f"{{resolver_id: {_quote(record.resolver_id)}}}]->(c)"
        )
        lines.append(
            "SET r += "
            + _props(
                [
                    ("resolver_version", record.resolver_version),
                    ("resolved_at", record.resolved_at.isoformat()),
                    ("reason", record.reason),
                ]
            )
            + ";"
        )

    return "\n".join(lines).rstrip() + "\n"
