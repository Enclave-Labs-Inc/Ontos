"""JSON-LD exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import json

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Fact
from ontos.storage.models import GraphScan, StoredEntity

_CONTEXT: dict[str, object] = {
    "@vocab": "urn:ontos:",
    "rawId": "urn:ontos:rawId",
    "canonicalName": "urn:ontos:canonicalName",
    "aliases": "urn:ontos:aliases",
    "properties": "urn:ontos:properties",
    "provenanceSourceId": "urn:ontos:provenanceSourceId",
    "provenanceExtractorId": "urn:ontos:provenanceExtractorId",
    "aclRef": "urn:ontos:aclRef",
    "subject": {"@id": "urn:ontos:subject", "@type": "@id"},
    "predicate": "urn:ontos:predicate",
    "object": {"@id": "urn:ontos:object", "@type": "@id"},
    "sourceId": "urn:ontos:sourceId",
    "extractorId": "urn:ontos:extractorId",
    "extractorVersion": "urn:ontos:extractorVersion",
    "confidence": "urn:ontos:confidence",
    "confidenceScore": "urn:ontos:confidenceScore",
    "tValid": "urn:ontos:tValid",
    "tInvalid": "urn:ontos:tInvalid",
    "ingestedAt": "urn:ontos:ingestedAt",
    "supersededBy": {"@id": "urn:ontos:supersededBy", "@type": "@id"},
    "resolverId": "urn:ontos:resolverId",
    "resolverVersion": "urn:ontos:resolverVersion",
    "resolvedAt": "urn:ontos:resolvedAt",
    "reason": "urn:ontos:reason",
}


def _entity_node(entity: StoredEntity) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": entity.id,
        "@type": entity.type or "Entity",
        "rawId": entity.id,
        "aliases": list(entity.aliases),
        "properties": dict(entity.properties),
    }
    if entity.canonical_name is not None:
        node["canonicalName"] = entity.canonical_name
    if entity.provenance_source_id is not None:
        node["provenanceSourceId"] = entity.provenance_source_id
    if entity.provenance_extractor_id is not None:
        node["provenanceExtractorId"] = entity.provenance_extractor_id
    if entity.acl_ref is not None:
        node["aclRef"] = entity.acl_ref
    return node


def _fact_node(fact: Fact) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": f"urn:ontos:fact:{fact.id}",
        "@type": "Fact",
        "subject": {"@id": fact.subject_id},
        "predicate": fact.predicate,
        "object": {"@id": fact.object_id},
        "sourceId": fact.provenance.source_id,
        "extractorId": fact.provenance.extractor_id,
        "extractorVersion": fact.provenance.extractor_version,
        "confidence": fact.provenance.confidence.value,
        "confidenceScore": fact.provenance.confidence_score,
        "tValid": fact.t_valid.isoformat(),
        "ingestedAt": fact.ingested_at.isoformat(),
    }
    if fact.t_invalid is not None:
        node["tInvalid"] = fact.t_invalid.isoformat()
    if fact.superseded_by is not None:
        node["supersededBy"] = {"@id": f"urn:ontos:fact:{fact.superseded_by}"}
    if fact.acl_ref is not None:
        node["aclRef"] = fact.acl_ref
    return node


def _merge_nodes(scan: GraphScan) -> list[dict[str, object]]:
    edges: list[tuple[str, str, str, MergeRecord]] = []
    for record in scan.merges:
        for merged_id in record.merged_ids:
            edges.append((record.canonical_id, merged_id, record.resolver_id, record))

    edges.sort(
        key=lambda item: (item[0], item[1], item[2], item[3].resolved_at.isoformat())
    )

    nodes: list[dict[str, object]] = []
    for index, (canonical_id, merged_id, _resolver_id, record) in enumerate(edges):
        nodes.append(
            {
                "@id": f"urn:ontos:merge:{index}",
                "@type": "Merge",
                "subject": {"@id": merged_id},
                "predicate": "MERGED_WITH",
                "object": {"@id": canonical_id},
                "resolverId": record.resolver_id,
                "resolverVersion": record.resolver_version,
                "resolvedAt": record.resolved_at.isoformat(),
                "reason": record.reason,
            }
        )
    return nodes


def render_jsonld(scan: GraphScan) -> str:
    """Render a deterministic JSON-LD document from a ``GraphScan``."""

    graph: list[dict[str, object]] = []

    for entity in sorted(scan.entities, key=lambda item: item.id):
        graph.append(_entity_node(entity))

    for fact in sorted(scan.facts, key=lambda item: str(item.id)):
        graph.append(_fact_node(fact))

    graph.extend(_merge_nodes(scan))

    document = {
        "@context": _CONTEXT,
        "@graph": graph,
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
