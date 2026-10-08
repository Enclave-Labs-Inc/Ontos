"""Cypher exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
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
            ("entity_type", entity.type),
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



_QUOTED = r"'(?:\\.|[^'\\])*'"

_ENTITY_LINE_RE = re.compile(
    rf"^MERGE \(n:`(?P<label>[^`]*)` "
    rf"\{{id: (?P<entity_id>{_QUOTED})\}}\) "
    rf"SET n \+= (?P<props>\{{.*\}});$"
)
_FACT_MATCH_RE = re.compile(
    rf"^MATCH \(s:`[^`]*` \{{id: (?P<subject>{_QUOTED})\}}\), "
    rf"\(o:`[^`]*` \{{id: (?P<object>{_QUOTED})\}}\)$"
)
_FACT_MERGE_RE = re.compile(
    rf"^MERGE \(s\)-\[r:`[^`]*` "
    rf"\{{fact_id: (?P<fact_id>{_QUOTED})\}}\]->\(o\)$"
)
_MERGE_MATCH_RE = re.compile(
    rf"^MATCH \(m:`[^`]*` \{{id: (?P<merged>{_QUOTED})\}}\), "
    rf"\(c:`[^`]*` \{{id: (?P<canonical>{_QUOTED})\}}\)$"
)
_MERGE_EDGE_RE = re.compile(
    rf"^MERGE \(m\)-\[r:MERGED_WITH "
    rf"\{{resolver_id: (?P<resolver_id>{_QUOTED})\}}\]->\(c\)$"
)


def _parse_literal(value: str) -> object:
    if value == "null":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"Unsupported Cypher literal: {value!r}") from exc


def _split_props(body: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                in_string = False
            continue

        if char == "'":
            in_string = True
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index].strip())
            start = index + 1

    final = body[start:].strip()
    if final:
        parts.append(final)
    return parts


def _parse_props(value: str) -> dict[str, object]:
    if not value.startswith("{") or not value.endswith("}"):
        raise ValueError("Ontos Cypher property map must use {...}")

    body = value[1:-1].strip()
    if not body:
        return {}

    result: dict[str, object] = {}
    for item in _split_props(body):
        if ":" not in item:
            raise ValueError(f"Invalid Ontos Cypher property: {item!r}")
        key, raw_value = item.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid Ontos Cypher property key: {key!r}")
        result[key] = _parse_literal(raw_value.strip())
    return result


def _required_str(props: dict[str, object], key: str) -> str:
    value = props.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Ontos Cypher property {key!r} must be a string")
    return value


def _optional_str(props: dict[str, object], key: str) -> str | None:
    value = props.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Ontos Cypher property {key!r} must be a string")
    return value


def load_cypher(path: Path) -> GraphScan:
    """Parse only the deterministic Cypher subset emitted by ``render_cypher``.

    The snapshot loader never executes Cypher. Arbitrary Cypher statements
    are rejected rather than being sent to Neo4j.
    """

    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]

    entities: list[StoredEntity] = []
    facts: list[Fact] = []
    merge_groups: dict[
        tuple[str, str, str, datetime, str],
        set[str],
    ] = {}

    index = 0
    while index < len(lines):
        line = lines[index]

        if line.startswith("CREATE INDEX IF NOT EXISTS "):
            index += 1
            continue

        entity_match = _ENTITY_LINE_RE.fullmatch(line)
        if entity_match is not None:
            entity_id = _parse_literal(entity_match.group("entity_id"))
            if not isinstance(entity_id, str):
                raise ValueError("Ontos Cypher entity id must be a string")

            props = _parse_props(entity_match.group("props"))

            aliases_raw = props.get("aliases", [])
            if not isinstance(aliases_raw, list):
                raise ValueError("Ontos Cypher aliases must be a list")

            properties_raw = props.get("properties", "{}")
            if not isinstance(properties_raw, str):
                raise ValueError("Ontos Cypher properties must be JSON text")
            properties = json.loads(properties_raw)
            if not isinstance(properties, dict):
                raise ValueError("Ontos Cypher properties must decode to an object")

            entities.append(
                StoredEntity(
                    id=entity_id,
                    type=_optional_str(props, "entity_type"),
                    canonical_name=_optional_str(props, "canonical_name"),
                    aliases=[str(alias) for alias in aliases_raw],
                    properties={
                        str(key): str(value)
                        for key, value in properties.items()
                    },
                    provenance_source_id=_optional_str(
                        props, "provenance_source_id"
                    ),
                    provenance_extractor_id=_optional_str(
                        props, "provenance_extractor_id"
                    ),
                    acl_ref=_optional_str(props, "acl_ref"),
                )
            )
            index += 1
            continue

        fact_match = _FACT_MATCH_RE.fullmatch(line)
        if fact_match is not None:
            if index + 2 >= len(lines):
                raise ValueError("Incomplete Ontos Cypher fact block")

            fact_merge = _FACT_MERGE_RE.fullmatch(lines[index + 1])
            set_line = lines[index + 2]
            if fact_merge is None or not set_line.startswith("SET r += "):
                raise ValueError("Invalid Ontos Cypher fact block")
            if not set_line.endswith(";"):
                raise ValueError("Ontos Cypher SET statement must end with ';'")

            subject = _parse_literal(fact_match.group("subject"))
            object_id = _parse_literal(fact_match.group("object"))
            fact_id = _parse_literal(fact_merge.group("fact_id"))
            if not all(isinstance(value, str) for value in (subject, object_id, fact_id)):
                raise ValueError("Ontos Cypher fact ids must be strings")

            props = _parse_props(set_line.removeprefix("SET r += ")[:-1])
            superseded_raw = _optional_str(props, "superseded_by")

            facts.append(
                Fact(
                    id=UUID(fact_id),
                    subject_id=subject,
                    predicate=_required_str(props, "predicate"),
                    object_id=object_id,
                    provenance=Provenance(
                        source_id=_required_str(props, "source_id"),
                        extractor_id=_required_str(props, "extractor_id"),
                        extractor_version=_required_str(props, "extractor_version"),
                        ontology_id=_required_str(props, "ontology_id"),
                        ontology_version=_required_str(props, "ontology_version"),
                        confidence=Confidence(_required_str(props, "confidence")),
                        confidence_score=float(props["confidence_score"]),
                    ),
                    t_valid=datetime.fromisoformat(
                        _required_str(props, "t_valid")
                    ),
                    t_invalid=(
                        datetime.fromisoformat(
                            _required_str(props, "t_invalid")
                        )
                        if props.get("t_invalid") is not None
                        else None
                    ),
                    ingested_at=datetime.fromisoformat(
                        _required_str(props, "ingested_at")
                    ),
                    superseded_by=(
                        UUID(superseded_raw)
                        if superseded_raw is not None
                        else None
                    ),
                    acl_ref=_optional_str(props, "acl_ref"),
                )
            )
            index += 3
            continue

        merge_match = _MERGE_MATCH_RE.fullmatch(line)
        if merge_match is not None:
            if index + 2 >= len(lines):
                raise ValueError("Incomplete Ontos Cypher merge block")

            merge_edge = _MERGE_EDGE_RE.fullmatch(lines[index + 1])
            set_line = lines[index + 2]
            if merge_edge is None or not set_line.startswith("SET r += "):
                raise ValueError("Invalid Ontos Cypher merge block")
            if not set_line.endswith(";"):
                raise ValueError("Ontos Cypher SET statement must end with ';'")

            merged_id = _parse_literal(merge_match.group("merged"))
            canonical_id = _parse_literal(merge_match.group("canonical"))
            resolver_id = _parse_literal(merge_edge.group("resolver_id"))
            if not all(
                isinstance(value, str)
                for value in (merged_id, canonical_id, resolver_id)
            ):
                raise ValueError("Ontos Cypher merge ids must be strings")

            props = _parse_props(set_line.removeprefix("SET r += ")[:-1])
            resolver_version = _required_str(props, "resolver_version")
            resolved_at = datetime.fromisoformat(
                _required_str(props, "resolved_at")
            )
            reason = _required_str(props, "reason")

            key = (
                canonical_id,
                resolver_id,
                resolver_version,
                resolved_at,
                reason,
            )
            merge_groups.setdefault(key, set()).add(merged_id)
            index += 3
            continue

        raise ValueError(
            "Unsupported Cypher statement. "
            "Snapshot loading accepts only output produced by render_cypher()."
        )

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
                    ("ontology_id", fact.provenance.ontology_id),
                    ("ontology_version", fact.provenance.ontology_version),
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
