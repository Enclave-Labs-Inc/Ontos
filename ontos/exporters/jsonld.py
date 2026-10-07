"""JSON-LD exporter for backend-neutral graph snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
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
    "ontologyId": "urn:ontos:ontologyId",
    "ontologyVersion": "urn:ontos:ontologyVersion",
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
        "ontologyId": fact.provenance.ontology_id,
        "ontologyVersion": fact.provenance.ontology_version,
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



def _ref_id(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("@id"), str):
        raise ValueError("JSON-LD reference must contain a string @id")
    return value["@id"]


def load_jsonld(path: Path) -> tuple[GraphScan, dict[str, str]]:
    """Load a #32 JSON-LD snapshot into the backend-neutral graph model."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("JSON-LD snapshot root must be an object")

    graph = document.get("@graph")
    if not isinstance(graph, list):
        raise ValueError("JSON-LD snapshot must contain an @graph array")

    manifest_raw = document.get("manifest", {})
    if not isinstance(manifest_raw, dict):
        raise ValueError("JSON-LD manifest must be an object")

    manifest = {
        "ontologyId": str(manifest_raw.get("ontologyId", "")),
        "ontologyVersion": str(manifest_raw.get("ontologyVersion", "")),
    }

    entities: list[StoredEntity] = []
    facts: list[Fact] = []

    merge_groups: dict[
        tuple[str, str, str, datetime, str],
        set[str],
    ] = {}

    for item in graph:
        if not isinstance(item, dict):
            raise ValueError("JSON-LD @graph entries must be objects")

        item_type = item.get("@type")

        if item_type == "Fact":
            fact_id_raw = item.get("@id")
            if not isinstance(fact_id_raw, str) or not fact_id_raw.startswith(
                "urn:ontos:fact:"
            ):
                raise ValueError("Fact @id must use urn:ontos:fact:<uuid>")

            superseded_by: UUID | None = None
            superseded_raw = item.get("supersededBy")
            if superseded_raw is not None:
                superseded_id = _ref_id(superseded_raw)
                prefix = "urn:ontos:fact:"
                if not superseded_id.startswith(prefix):
                    raise ValueError("supersededBy must reference an Ontos fact")
                superseded_by = UUID(superseded_id.removeprefix(prefix))

            facts.append(
                Fact(
                    id=UUID(fact_id_raw.removeprefix("urn:ontos:fact:")),
                    subject_id=_ref_id(item.get("subject")),
                    predicate=str(item["predicate"]),
                    object_id=_ref_id(item.get("object")),
                    provenance=Provenance(
                        source_id=str(item["sourceId"]),
                        extractor_id=str(item["extractorId"]),
                        extractor_version=str(item["extractorVersion"]),
                        ontology_id=str(item.get("ontologyId", "")),
                        ontology_version=str(item.get("ontologyVersion", "")),
                        confidence=Confidence(str(item["confidence"])),
                        confidence_score=float(item["confidenceScore"]),
                    ),
                    t_valid=datetime.fromisoformat(str(item["tValid"])),
                    t_invalid=(
                        datetime.fromisoformat(str(item["tInvalid"]))
                        if item.get("tInvalid") is not None
                        else None
                    ),
                    ingested_at=datetime.fromisoformat(str(item["ingestedAt"])),
                    superseded_by=superseded_by,
                    acl_ref=(
                        str(item["aclRef"])
                        if item.get("aclRef") is not None
                        else None
                    ),
                )
            )
            continue

        if item_type == "Merge":
            if item.get("predicate") != "MERGED_WITH":
                raise ValueError("Merge entry must use predicate MERGED_WITH")

            merged_id = _ref_id(item.get("subject"))
            canonical_id = _ref_id(item.get("object"))
            resolved_at = datetime.fromisoformat(str(item["resolvedAt"]))
            resolver_id = str(item["resolverId"])
            resolver_version = str(item["resolverVersion"])
            reason = str(item.get("reason", ""))

            key = (
                canonical_id,
                resolver_id,
                resolver_version,
                resolved_at,
                reason,
            )
            merge_groups.setdefault(key, set()).add(merged_id)
            continue

        entity_id = item.get("rawId", item.get("@id"))
        if not isinstance(entity_id, str):
            raise ValueError("Entity entry must contain rawId or string @id")

        aliases_raw = item.get("aliases", [])
        properties_raw = item.get("properties", {})
        if not isinstance(aliases_raw, list):
            raise ValueError("Entity aliases must be a list")
        if not isinstance(properties_raw, dict):
            raise ValueError("Entity properties must be an object")

        entities.append(
            StoredEntity(
                id=entity_id,
                type=str(item_type) if item_type is not None else None,
                canonical_name=(
                    str(item["canonicalName"])
                    if item.get("canonicalName") is not None
                    else None
                ),
                aliases=[str(alias) for alias in aliases_raw],
                properties={
                    str(key): str(value)
                    for key, value in properties_raw.items()
                },
                provenance_source_id=(
                    str(item["provenanceSourceId"])
                    if item.get("provenanceSourceId") is not None
                    else None
                ),
                provenance_extractor_id=(
                    str(item["provenanceExtractorId"])
                    if item.get("provenanceExtractorId") is not None
                    else None
                ),
                acl_ref=(
                    str(item["aclRef"])
                    if item.get("aclRef") is not None
                    else None
                ),
            )
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

    return (
        GraphScan(
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
        ),
        manifest,
    )

def render_jsonld(scan: GraphScan) -> str:
    """Render a deterministic JSON-LD document from a ``GraphScan``."""

    graph: list[dict[str, object]] = []

    for entity in sorted(scan.entities, key=lambda item: item.id):
        graph.append(_entity_node(entity))

    for fact in sorted(scan.facts, key=lambda item: str(item.id)):
        graph.append(_fact_node(fact))

    graph.extend(_merge_nodes(scan))

    first_fact = min(scan.facts, key=lambda item: str(item.id), default=None)
    manifest = {
        "ontologyId": first_fact.provenance.ontology_id if first_fact is not None else "",
        "ontologyVersion": (
            first_fact.provenance.ontology_version if first_fact is not None else ""
        ),
    }

    document = {
        "@context": _CONTEXT,
        "manifest": manifest,
        "@graph": graph,
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
