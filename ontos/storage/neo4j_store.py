"""Neo4j GraphStore — the production storage backend for M1+.

Cypher patterns follow `docs/design/m1.md`:

- Entities are `(:Entity)` nodes keyed on `id`. `type` is stored as a
  property in M1.b; upgrading to additive labels (`:Entity:Person`) is
  a follow-up once the ontology whitelist is enforced (label creation
  from user input is a small attack surface we're deliberately avoiding
  until the whitelist is a hard check).
- Facts are `[:RELATES]` edges with `predicate` as a property so the
  schema stays stable as the ontology evolves. Fact metadata
  (provenance, bitemporal, ACL) lives on the edge.
- Bitemporal filters use `t_valid` / `t_invalid`; the composite index
  makes this the hot path.
- Traversal enforces permissions via `ALL(rel IN rels WHERE ...)`, so
  forbidden edges prune paths at query time and target nodes never
  materialize — no existence leak.
- Corrections are supersession, never in-place mutation.

Indexes and constraints are created idempotently on `initialize()`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.time import DateTime as Neo4jDateTime

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.base import CrossAclUpsertError

log = structlog.get_logger(__name__)

# Cap traversal depth at the store boundary so a malformed caller can't
# ask Neo4j for a billion-hop expansion. Callers may still cap lower.
_MAX_TRAVERSAL_DEPTH = 10


def _fact_to_edge_props(fact: Fact) -> dict[str, Any]:
    """Flatten a Fact into the property bag we persist on the edge."""
    return {
        "fact_id": str(fact.id),
        "predicate": fact.predicate,
        "t_valid": fact.t_valid,
        "t_invalid": fact.t_invalid,
        "ingested_at": fact.ingested_at,
        "superseded_by": str(fact.superseded_by) if fact.superseded_by else None,
        "acl_ref": fact.acl_ref,
        "prov_source_id": fact.provenance.source_id,
        "prov_extractor_id": fact.provenance.extractor_id,
        "prov_extractor_version": fact.provenance.extractor_version,
        "prov_ontology_id": fact.provenance.ontology_id,
        "prov_ontology_version": fact.provenance.ontology_version,
        "prov_confidence": fact.provenance.confidence.value,
        "prov_confidence_score": fact.provenance.confidence_score,
    }


def _edge_props_to_fact(subject_id: str, object_id: str, props: dict[str, Any]) -> Fact:
    """Reconstruct a Fact from the edge props Neo4j returned."""
    return Fact(
        id=UUID(props["fact_id"]),
        subject_id=subject_id,
        object_id=object_id,
        predicate=props["predicate"],
        provenance=Provenance(
            source_id=props["prov_source_id"],
            extractor_id=props["prov_extractor_id"],
            extractor_version=props["prov_extractor_version"],
            # #34: back-compat — pre-#34 edges have no ontology props;
            # default to "" so legacy facts load with the sentinel the
            # MigrationRegistry treats as "unstamped".
            ontology_id=props.get("prov_ontology_id", "") or "",
            ontology_version=props.get("prov_ontology_version", "") or "",
            confidence=Confidence(props["prov_confidence"]),
            confidence_score=props["prov_confidence_score"],
        ),
        t_valid=_to_python_datetime(props["t_valid"]),
        t_invalid=_to_python_datetime(props.get("t_invalid")),
        ingested_at=_to_python_datetime(props["ingested_at"]),
        superseded_by=UUID(props["superseded_by"]) if props.get("superseded_by") else None,
        acl_ref=props.get("acl_ref"),
    )


def _to_python_datetime(value: Any) -> Any:
    """Neo4j returns its own DateTime type; convert to stdlib for Pydantic."""
    if value is None:
        return None
    if isinstance(value, Neo4jDateTime):
        return value.to_native()
    return value


class Neo4jStore:
    """`GraphStore` against Neo4j. Requires Neo4j 5.x for its datetime types
    and its native support for parameterized variable-length paths."""

    def __init__(
        self,
        driver: AsyncDriver,
        *,
        database: str = "neo4j",
    ) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def from_uri(
        cls,
        uri: str,
        *,
        auth: tuple[str, str],
        database: str = "neo4j",
    ) -> Neo4jStore:
        driver = AsyncGraphDatabase.driver(uri, auth=auth)
        return cls(driver, database=database)

    async def initialize(self) -> None:
        """Create indexes idempotently. Safe to call on every boot."""
        statements = [
            "CREATE INDEX entity_id_idx IF NOT EXISTS FOR (n:Entity) ON (n.id)",
            # #38: type-filtered reads (e.g. #32's export "all Person nodes")
            # stay O(matching-nodes) on 10k+ node graphs with this index.
            "CREATE INDEX entity_type_idx IF NOT EXISTS FOR (n:Entity) ON (n.type)",
            "CREATE INDEX fact_id_idx IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.fact_id)",
            "CREATE INDEX fact_predicate_idx IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.predicate)",
            "CREATE INDEX fact_validity_idx IF NOT EXISTS "
            "FOR ()-[r:RELATES]-() ON (r.t_valid, r.t_invalid)",
            # #46: audit queries on the merge trail scan by resolver +
            # time. [:MERGED_WITH] is a different edge type from facts;
            # separate index keeps merge audit and fact traversal
            # performance decoupled.
            "CREATE INDEX merge_record_idx IF NOT EXISTS "
            "FOR ()-[r:MERGED_WITH]-() ON (r.resolver_id, r.resolved_at)",
        ]
        async with self._driver.session(database=self._database) as session:
            for stmt in statements:
                await session.run(stmt)

    async def add_fact(self, fact: Fact) -> None:
        query = """
        MERGE (a:Entity {id: $subject_id})
          ON CREATE SET a.canonical_name = $subject_id
        MERGE (b:Entity {id: $object_id})
          ON CREATE SET b.canonical_name = $object_id
        CREATE (a)-[r:RELATES $props]->(b)
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query,
                subject_id=fact.subject_id,
                object_id=fact.object_id,
                props=_fact_to_edge_props(fact),
            )

    async def upsert_entity(self, entity: Entity, *, acl_ref: str | None = None) -> None:
        # #38: idempotent MERGE on (:Entity {id}). Last-write-wins on
        # type / canonical_name / aliases / properties when the ACL
        # doesn't change. IngestPipeline calls this before add_fact so
        # every fact's subject_id / object_id has a backing typed node.
        #
        # Neo4j property values must be primitives or arrays of
        # primitives. `Entity.properties: dict[str, str]` is a map,
        # which Neo4j rejects as a node property — JSON-encode on
        # write; readers decode via `json.loads(n.properties)`.
        #
        # #48: two-query transaction so the cross-ACL check runs
        # BEFORE any write. If the raise fires, the transaction rolls
        # back and the node's prior attrs stay intact (no partial
        # write). session.execute_write wraps both tx.runs in a
        # single Neo4j transaction so there's no TOCTOU window for
        # another writer.
        properties_json = json.dumps(dict(entity.properties), sort_keys=True)

        async def _upsert_tx(tx: Any) -> str | None:
            # PR #49 review: MERGE + ON MATCH SET acquires the write
            # lock on `n` BEFORE we read its acl_ref. Pre-review the
            # plain OPTIONAL MATCH took no lock, so two concurrent
            # writers could both see prev_acl_ref=None, both pass the
            # check, and the second would overwrite the first's ACL
            # via coalesce($prev_acl_ref=None, $acl_ref) — a race-
            # induced reintroduction of the #48 bug. The _upsert_count
            # bump is a dedicated lock-acquisition write; it also
            # gives operators a per-node upsert counter for free.
            prev_result = await tx.run(
                """
                MERGE (n:Entity {id: $id})
                ON CREATE SET n._upsert_count = 1
                ON MATCH SET n._upsert_count = coalesce(n._upsert_count, 0) + 1
                WITH n, n.type AS prev_type, n.acl_ref AS prev_acl_ref
                RETURN prev_type, prev_acl_ref
                """,
                id=entity.id,
            )
            prev_row = await prev_result.single()
            prev_type: str | None = prev_row["prev_type"] if prev_row else None
            prev_acl_ref: str | None = prev_row["prev_acl_ref"] if prev_row else None

            # #48 cross-ACL check — raise inside the transaction so
            # the whole thing rolls back; nothing writes if the ACL
            # would change on an already-stamped entity.
            if prev_acl_ref is not None and prev_acl_ref != acl_ref:
                raise CrossAclUpsertError(
                    entity_id=entity.id,
                    stored_acl=prev_acl_ref,
                    attempted_acl=acl_ref,
                )

            await tx.run(
                """
                MERGE (n:Entity {id: $id})
                SET n.type = $type,
                    n.canonical_name = $canonical_name,
                    n.aliases = $aliases,
                    n.properties = $properties_json,
                    n.provenance_source_id = $provenance_source_id,
                    n.provenance_extractor_id = $provenance_extractor_id,
                    n.acl_ref = coalesce($prev_acl_ref, $acl_ref)
                """,
                id=entity.id,
                type=entity.type,
                canonical_name=entity.canonical_name,
                aliases=list(entity.aliases),
                properties_json=properties_json,
                provenance_source_id=entity.provenance.source_id,
                provenance_extractor_id=entity.provenance.extractor_id,
                prev_acl_ref=prev_acl_ref,
                acl_ref=acl_ref,
            )
            return prev_type

        try:
            async with self._driver.session(database=self._database) as session:
                prev_type = await session.execute_write(_upsert_tx)
        except CrossAclUpsertError as exc:
            # Also log the structured audit event (parity with
            # NetworkxStore). Compliance operators grep logs rather
            # than parsing exception traces.
            # PR #49 review: do NOT log attempted_canonical_name — it
            # can be restricted attribute content from the attempting
            # doc's ACL scope, and log sinks are typically readable
            # without those ACLs. Ids + ACL refs only.
            log.warning(
                "cross-acl-upsert-rejected",
                entity_id=entity.id,
                stored_acl=exc.stored_acl,
                attempted_acl=exc.attempted_acl,
            )
            raise

        if prev_type is not None and prev_type != entity.type:
            log.warning(
                "entity-type-overwrite",
                entity_id=entity.id,
                previous_type=prev_type,
                new_type=entity.type,
                canonical_name=entity.canonical_name,
            )

    async def close_fact(
        self,
        fact_id: UUID,
        t_invalid: datetime,
        superseded_by: UUID | None = None,
    ) -> None:
        query = """
        MATCH ()-[r:RELATES {fact_id: $fact_id}]->()
        SET r.t_invalid = $t_invalid,
            r.superseded_by = $superseded_by
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query,
                fact_id=str(fact_id),
                t_invalid=t_invalid,
                superseded_by=str(superseded_by) if superseded_by else None,
            )

    async def get_fact(self, fact_id: UUID) -> Fact | None:
        query = """
        MATCH (a:Entity)-[r:RELATES {fact_id: $fact_id}]->(b:Entity)
        RETURN a.id AS subject_id, b.id AS object_id, properties(r) AS props
        LIMIT 1
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, fact_id=str(fact_id))
            record = await result.single()
            if record is None:
                return None
            return _edge_props_to_fact(record["subject_id"], record["object_id"], record["props"])

    async def search(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        k: int = 10,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]:
        # M1.b uses substring match — mirrors NetworkxStore for parity.
        # M3 upgrades to full-text + vector hybrid.
        cypher = """
        MATCH (a:Entity)-[r:RELATES]->(b:Entity)
        WHERE
            (
                ($as_of IS NULL AND r.t_invalid IS NULL)
                OR (
                    $as_of IS NOT NULL
                    AND r.t_valid <= $as_of
                    AND (r.t_invalid IS NULL OR r.t_invalid > $as_of)
                )
            )
            AND (
                r.acl_ref IS NULL
                OR ($allowed_acls IS NOT NULL AND r.acl_ref IN $allowed_acls)
                OR ($allowed_acls IS NULL AND $acl_subject IS NOT NULL
                    AND r.acl_ref = $acl_subject)
            )
            AND toLower(coalesce(a.canonical_name, "") + " "
                        + r.predicate + " "
                        + coalesce(b.canonical_name, "")) CONTAINS toLower($needle)
        RETURN a.id AS subject_id, b.id AS object_id, properties(r) AS props
        LIMIT $k
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                cypher,
                needle=query,
                as_of=as_of,
                acl_subject=acl_subject,
                allowed_acls=allowed_acls,
                k=k,
            )
            records = await result.data()
        return [_edge_props_to_fact(r["subject_id"], r["object_id"], r["props"]) for r in records]

    async def traverse(
        self,
        start: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
        depth: int = 2,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> Iterable[Fact]:
        # Depth is interpolated (not parameterized) because Cypher's
        # variable-length pattern syntax requires literal bounds. We
        # cap it defensively before interpolation.
        depth = max(1, min(depth, _MAX_TRAVERSAL_DEPTH))

        path_patterns = {
            "out": f"(start:Entity {{id: $start_id}})-[rels:RELATES*1..{depth}]->(end:Entity)",
            "in": f"(start:Entity {{id: $start_id}})<-[rels:RELATES*1..{depth}]-(end:Entity)",
            "both": f"(start:Entity {{id: $start_id}})-[rels:RELATES*1..{depth}]-(end:Entity)",
        }
        path_pattern = path_patterns[direction]

        cypher = f"""
        MATCH path = {path_pattern}
        WHERE ALL(r IN rels WHERE
            (
                ($as_of IS NULL AND r.t_invalid IS NULL)
                OR (
                    $as_of IS NOT NULL
                    AND r.t_valid <= $as_of
                    AND (r.t_invalid IS NULL OR r.t_invalid > $as_of)
                )
            )
            AND (
                r.acl_ref IS NULL
                OR ($allowed_acls IS NOT NULL AND r.acl_ref IN $allowed_acls)
                OR ($allowed_acls IS NULL AND $acl_subject IS NOT NULL
                    AND r.acl_ref = $acl_subject)
            )
            AND ($relation IS NULL OR r.predicate = $relation)
        )
        UNWIND rels AS r
        WITH DISTINCT r, startNode(r) AS a, endNode(r) AS b
        RETURN a.id AS subject_id, b.id AS object_id, properties(r) AS props
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                cypher,
                start_id=start,
                as_of=as_of,
                acl_subject=acl_subject,
                allowed_acls=allowed_acls,
                relation=relation,
            )
            records = await result.data()
        return [_edge_props_to_fact(r["subject_id"], r["object_id"], r["props"]) for r in records]

    async def facts_for_entity(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]:
        cypher = """
        MATCH (n:Entity {id: $entity_id})-[r:RELATES]-(other:Entity)
        WHERE
            (
                ($as_of IS NULL AND r.t_invalid IS NULL)
                OR (
                    $as_of IS NOT NULL
                    AND r.t_valid <= $as_of
                    AND (r.t_invalid IS NULL OR r.t_invalid > $as_of)
                )
            )
            AND (
                r.acl_ref IS NULL
                OR ($allowed_acls IS NOT NULL AND r.acl_ref IN $allowed_acls)
                OR ($allowed_acls IS NULL AND $acl_subject IS NOT NULL
                    AND r.acl_ref = $acl_subject)
            )
        RETURN startNode(r).id AS subject_id,
               endNode(r).id AS object_id,
               properties(r) AS props
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                cypher,
                entity_id=entity_id,
                as_of=as_of,
                acl_subject=acl_subject,
                allowed_acls=allowed_acls,
            )
            records = await result.data()
        return [_edge_props_to_fact(r["subject_id"], r["object_id"], r["props"]) for r in records]

    async def entity_acl(self, entity_id: str) -> str | None:
        # PR #49: read-only ACL lookup used by IngestPipeline to
        # pre-check all extracted entities before any write.
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "OPTIONAL MATCH (n:Entity {id: $id}) RETURN n.acl_ref AS acl",
                id=entity_id,
            )
            row = await result.single()
        if row is None:
            return None
        acl = row["acl"]
        return acl if isinstance(acl, str) else None

    async def record_merge(self, record: MergeRecord) -> None:
        # #46: materialize MergeRecord as [:MERGED_WITH] edges from each
        # merged_id to the canonical. MERGE on (canonical, merged,
        # resolver_id) gives us the idempotency key the Protocol
        # contract promises; SET overwrites resolved_at + reason on
        # re-assertion (last-write-wins on the audit timestamp).
        #
        # PR #47 review: single UNWIND statement so a mid-loop failure
        # can't leave a partial merge record (edges for B but not C).
        # One transaction, one atomic write per logical MergeRecord.
        query = """
        MERGE (canonical:Entity {id: $canonical_id})
        WITH canonical
        UNWIND $merged_ids AS merged_id_param
        WITH canonical, merged_id_param
        WHERE merged_id_param <> $canonical_id
        MERGE (merged:Entity {id: merged_id_param})
        MERGE (merged)-[r:MERGED_WITH {resolver_id: $resolver_id}]->(canonical)
        SET r.resolver_version = $resolver_version,
            r.resolved_at = datetime($resolved_at),
            r.reason = $reason
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query,
                canonical_id=record.canonical_id,
                merged_ids=list(record.merged_ids),
                resolver_id=record.resolver_id,
                resolver_version=record.resolver_version,
                resolved_at=record.resolved_at.isoformat(),
                reason=record.reason,
            )

    async def merges_for_entity(
        self,
        entity_id: str,
        *,
        allowed_acls: list[str] | None = None,
    ) -> list[MergeRecord]:
        # Bidirectional lookup — the id can appear as canonical or
        # merged.
        #
        # PR #47 review: two-step Cypher so the group is found first,
        # THEN all its edges are collected. Pre-review the one-step
        # pattern truncated merged_ids when queried from a merged
        # endpoint — graph A ← {B,C} queried on "B" returned
        # merged_ids=[B] (losing C), a real Article-12 audit gap.
        #
        # ACL pre-filter (PR #47 suggestion 2): optional allowed_acls
        # drops records where any endpoint entity has an acl_ref the
        # caller cannot see. CLAUDE.md "pre-filter when possible" —
        # safer default than relying on callers to post-filter.
        query = """
        // Step 1: find matching groups by their identifying tuple.
        MATCH (m:Entity)-[r:MERGED_WITH]->(c:Entity)
        WHERE c.id = $entity_id OR m.id = $entity_id
        WITH DISTINCT c,
             r.resolver_id AS resolver_id,
             r.resolver_version AS resolver_version,
             r.resolved_at AS resolved_at,
             r.reason AS reason
        // Step 2: collect every edge in each identified group.
        MATCH (m2:Entity)-[r2:MERGED_WITH {resolver_id: resolver_id}]->(c)
        WHERE r2.resolved_at = resolved_at
        WITH c, resolver_id, resolver_version, resolved_at, reason,
             collect(m2) AS merged_nodes
        // ACL pre-filter — canonical AND every endpoint must be visible.
        WHERE $allowed_acls IS NULL
           OR (
               (c.acl_ref IS NULL OR c.acl_ref IN $allowed_acls)
               AND ALL(mn IN merged_nodes
                       WHERE mn.acl_ref IS NULL OR mn.acl_ref IN $allowed_acls)
           )
        RETURN c.id AS canonical_id,
               [mn IN merged_nodes | mn.id] AS merged_ids,
               resolver_id, resolver_version, resolved_at, reason
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query,
                entity_id=entity_id,
                allowed_acls=allowed_acls,
            )
            records = await result.data()

        hits: list[MergeRecord] = []
        for row in records:
            resolved_at_raw = row["resolved_at"]
            # Neo4j returns its own DateTime; convert to stdlib datetime.
            if isinstance(resolved_at_raw, Neo4jDateTime):
                resolved_at = resolved_at_raw.to_native()
            else:
                resolved_at = resolved_at_raw
            hits.append(
                MergeRecord(
                    canonical_id=row["canonical_id"],
                    merged_ids=list(row["merged_ids"]),
                    resolver_id=row["resolver_id"],
                    resolver_version=row["resolver_version"],
                    resolved_at=resolved_at,
                    reason=row["reason"] or "",
                )
            )
        return hits

    async def close(self) -> None:
        await self._driver.close()
