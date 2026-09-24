"""FastMCP entry point.

Wires storage + audit + (later) planner/executor/authz behind the MCP tool surface.
Every tool call emits an Article-12 audit record before returning.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastmcp import FastMCP
from pydantic import BaseModel

from ontos.audit.emitter import AuditEmitter
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import AuditRecord, Fact
from ontos.storage.base import GraphStore
from ontos.storage.networkx_store import NetworkxStore

log = structlog.get_logger()


class ToolResponse(BaseModel):
    """Wraps every tool result with the audit record it produced.

    Agents receive both the payload and proof that the query was audited.
    """

    query_id: UUID
    audit_hash: str
    payload: Any


def build_store(config: RuntimeConfig) -> GraphStore:
    if config.storage_backend == "networkx":
        return NetworkxStore()
    raise NotImplementedError(
        f"backend {config.storage_backend!r} not wired in M0 — "
        "only 'networkx' is available. Neo4j lands in M1."
    )


def build_server(
    config: RuntimeConfig | None = None,
    *,
    store: GraphStore | None = None,
    audit: AuditEmitter | None = None,
) -> FastMCP:
    cfg = config if config is not None else RuntimeConfig.from_env()
    store = store if store is not None else build_store(cfg)
    audit = audit if audit is not None else AuditEmitter.from_env(cfg.audit_signing_key_env)

    mcp = FastMCP("ontos")

    def _emit(
        *,
        tool: str,
        agent_identity: str,
        acting_on_behalf_of: str | None,
        query_text: str,
        tool_arguments: dict[str, object],
        result_fact_ids: list[str],
        latency_ms: float,
        policy_decisions: list[dict[str, str]],
    ) -> AuditRecord:
        return audit.emit(
            query_id=None,
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query_text,
            tool_invoked=tool,
            tool_arguments=tool_arguments,
            result_fact_ids=result_fact_ids,
            latency_ms=latency_ms,
            model_versions={"runtime": "0.0.1", "backend": cfg.storage_backend},
            policy_decisions=policy_decisions,
        )

    @mcp.tool()
    async def search(
        query: str,
        agent_identity: str,
        acting_on_behalf_of: str | None = None,
        as_of: str | None = None,
        k: int = 10,
    ) -> ToolResponse:
        """Semantic + graph retrieval. Returns facts with per-fact provenance."""
        started = time.perf_counter()
        ts = datetime.fromisoformat(as_of) if as_of else None
        facts = await store.search(query, as_of=ts, k=k, acl_subject=acting_on_behalf_of)
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="search",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query,
            tool_arguments={"as_of": as_of, "k": k},
            result_fact_ids=[str(f.id) for f in facts],
            latency_ms=latency_ms,
            policy_decisions=[],
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload=[fact.model_dump(mode="json") for fact in facts],
        )

    @mcp.tool()
    async def traverse(
        start: str,
        agent_identity: str,
        relation: str | None = None,
        depth: int = 2,
        as_of: str | None = None,
        acting_on_behalf_of: str | None = None,
    ) -> ToolResponse:
        """Multi-hop traversal from an entity. Permission-aware during traversal."""
        started = time.perf_counter()
        ts = datetime.fromisoformat(as_of) if as_of else None
        facts = list(
            await store.traverse(
                start,
                relation=relation,
                depth=depth,
                as_of=ts,
                acl_subject=acting_on_behalf_of,
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="traverse",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=start,
            tool_arguments={"relation": relation, "depth": depth, "as_of": as_of},
            result_fact_ids=[str(f.id) for f in facts],
            latency_ms=latency_ms,
            policy_decisions=[],
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload=[fact.model_dump(mode="json") for fact in facts],
        )

    @mcp.tool()
    async def explain(
        entity_id: str,
        agent_identity: str,
        as_of: str | None = None,
        acting_on_behalf_of: str | None = None,
    ) -> ToolResponse:
        """Entity dossier: definition, sources, related entities, validity ranges."""
        started = time.perf_counter()
        ts = datetime.fromisoformat(as_of) if as_of else None
        facts = await store.facts_for_entity(
            entity_id, as_of=ts, acl_subject=acting_on_behalf_of
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="explain",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=entity_id,
            tool_arguments={"as_of": as_of},
            result_fact_ids=[str(f.id) for f in facts],
            latency_ms=latency_ms,
            policy_decisions=[],
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload=[fact.model_dump(mode="json") for fact in facts],
        )

    @mcp.tool()
    async def provenance(fact_id: str, agent_identity: str) -> ToolResponse:
        """Return the full provenance chain for one fact."""
        started = time.perf_counter()
        fact = await store.get_fact(UUID(fact_id))
        latency_ms = (time.perf_counter() - started) * 1000
        payload: Fact | None = fact
        record = _emit(
            tool="provenance",
            agent_identity=agent_identity,
            acting_on_behalf_of=None,
            query_text=fact_id,
            tool_arguments={},
            result_fact_ids=[fact_id] if fact else [],
            latency_ms=latency_ms,
            policy_decisions=[],
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload=payload.model_dump(mode="json") if payload else None,
        )

    @mcp.tool()
    async def audit_lookup(query_id: str, agent_identity: str) -> ToolResponse:
        """Return the Article-12 audit record for a prior query."""
        record = audit.by_query_id(UUID(query_id))
        emitted = _emit(
            tool="audit",
            agent_identity=agent_identity,
            acting_on_behalf_of=None,
            query_text=query_id,
            tool_arguments={},
            result_fact_ids=[],
            latency_ms=0.0,
            policy_decisions=[],
        )
        return ToolResponse(
            query_id=emitted.query_id,
            audit_hash=emitted.hash,
            payload=record.model_dump(mode="json") if record else None,
        )

    return mcp


def main() -> None:
    """CLI entry — boots the server with streamable-HTTP transport."""
    # Dev-only: default a signing key if operator did not set one; refuse in prod.
    if os.environ.get("ONTOS_ENV") != "prod" and not os.environ.get(
        "ONTOS_AUDIT_SIGNING_KEY"
    ):
        os.environ["ONTOS_AUDIT_SIGNING_KEY"] = "dev-only-signing-key-do-not-use"
        log.warning("using dev signing key — never do this in prod")

    cfg = RuntimeConfig.from_env()
    server = build_server(cfg)
    log.info(
        "ontos starting",
        host=cfg.listen_host,
        port=cfg.listen_port,
        backend=cfg.storage_backend,
    )
    server.run(transport="http", host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
