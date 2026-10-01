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

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.time import DateTime as Neo4jDateTime

from ontos.runtime.models import Confidence, Fact, Provenance

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
            "CREATE INDEX fact_id_idx IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.fact_id)",
            "CREATE INDEX fact_predicate_idx IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.predicate)",
            "CREATE INDEX fact_validity_idx IF NOT EXISTS "
            "FOR ()-[r:RELATES]-() ON (r.t_valid, r.t_invalid)",
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
        depth: int = 2,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> Iterable[Fact]:
        # Depth is interpolated (not parameterized) because Cypher's
        # variable-length pattern syntax requires literal bounds. We
        # cap it defensively before interpolation.
        depth = max(1, min(depth, _MAX_TRAVERSAL_DEPTH))
        cypher = f"""
        MATCH path = (start:Entity {{id: $start_id}})-[rels:RELATES*1..{depth}]->(end:Entity)
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

    async def close(self) -> None:
        await self._driver.close()
