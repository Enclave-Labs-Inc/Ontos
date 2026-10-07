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
from ontos.authz import VIEW, AuthzBackend, InMemoryAuthz
from ontos.executor import DeterministicExecutor, Executor
from ontos.planner import Planner, PlannerError
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
        return NetworkxStore(path=config.storage_path)
    if config.storage_backend == "neo4j":
        if config.storage_path is not None:
            log.warning(
                "storage-path-ignored-for-backend",
                backend="neo4j",
                storage_path=str(config.storage_path),
                message=(
                    "ONTOS_STORAGE_PATH / --storage-path is a NetworkxStore-only "
                    "concept and is ignored for the Neo4j backend."
                ),
            )
        # Deferred import: `neo4j` is an optional extra so the base install
        # doesn't drag it in for dev/CI runs that use the in-memory backend.
        from ontos.storage.neo4j_store import Neo4jStore

        uri = os.environ.get("NEO4J_URI")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD")
        if not uri or not password:
            raise RuntimeError("ONTOS_STORAGE_BACKEND=neo4j requires NEO4J_URI + NEO4J_PASSWORD")
        database = os.environ.get("NEO4J_DATABASE", "neo4j")
        return Neo4jStore.from_uri(uri, auth=(user, password), database=database)
    raise NotImplementedError(
        f"backend {config.storage_backend!r} is not wired — "
        "supported: 'networkx' (dev), 'neo4j'. Neptune/LadybugDB land in M2."
    )


def build_authz(config: RuntimeConfig) -> AuthzBackend | None:
    """Instantiate the authz backend for this runtime, if any.

    - unset / "none" → no backend; the store falls back to the M1
      `acl_subject == fact.acl_ref` shim.
    - "inmemory" → `InMemoryAuthz` for dev + tests; grants must be
      seeded programmatically.
    - "openfga" → `OpenFGAAuthz` (optional extra), configured via
      `FGA_API_URL`, `FGA_STORE_ID`, `FGA_API_TOKEN`.
    """
    backend = os.environ.get("ONTOS_AUTHZ_BACKEND", "").lower()
    if backend in ("", "none"):
        return None
    if backend == "inmemory":
        return InMemoryAuthz()
    if backend == "openfga":
        # Deferred import — openfga-sdk is an optional extra.
        from ontos.authz.openfga import OpenFGAAuthz

        return OpenFGAAuthz.from_env()
    raise RuntimeError(
        f"ONTOS_AUTHZ_BACKEND={backend!r} not supported. Use 'none', 'inmemory', or 'openfga'."
    )


def build_server(
    config: RuntimeConfig | None = None,
    *,
    store: GraphStore | None = None,
    audit: AuditEmitter | None = None,
    authz: AuthzBackend | None = None,
    planner: Planner | None = None,
    executor: Executor | None = None,
) -> FastMCP:
    cfg = config if config is not None else RuntimeConfig.from_env()
    store = store if store is not None else build_store(cfg)
    audit = audit if audit is not None else AuditEmitter.from_env(cfg.audit_signing_key_env)
    # Note: authz is opt-in — passing None disables authz and falls back
    # to the M1 `acl_subject == fact.acl_ref` shim in the store.
    resolved_authz: AuthzBackend | None = authz if authz is not None else build_authz(cfg)
    # Executor is always available (deterministic, no config). Planner is
    # opt-in — if no planner is wired, the `ask` tool is disabled (the
    # store tools still work). This lets M0/M1 deployments run without
    # an LLM planner backend.
    resolved_executor: Executor = executor if executor is not None else DeterministicExecutor()
    resolved_planner: Planner | None = planner

    async def _resolve_allowed_acls(
        subject: str | None,
    ) -> tuple[list[str] | None, list[dict[str, str]]]:
        """Ask the authz backend which acl_refs `subject` can view.

        Returns `(allowed_acls, policy_decisions)`. `allowed_acls` is
        `None` when no authz backend is configured (store falls back
        to M1 shim). `policy_decisions` is the audit-trail record of
        what the backend was asked and what it said.
        """
        if resolved_authz is None or subject is None:
            return None, []
        allowed = await resolved_authz.list_authorized_objects(subject, VIEW)
        return allowed, [
            {
                "policy": resolved_authz.id,
                "subject": subject,
                "relation": VIEW,
                "result": ",".join(allowed) if allowed else "(none)",
            }
        ]

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
        allowed_acls, policy_decisions = await _resolve_allowed_acls(acting_on_behalf_of)
        facts = await store.search(
            query,
            as_of=ts,
            k=k,
            acl_subject=acting_on_behalf_of,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="search",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query,
            tool_arguments={"as_of": as_of, "k": k},
            result_fact_ids=[str(f.id) for f in facts],
            latency_ms=latency_ms,
            policy_decisions=policy_decisions,
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
        allowed_acls, policy_decisions = await _resolve_allowed_acls(acting_on_behalf_of)
        facts = list(
            await store.traverse(
                start,
                relation=relation,
                depth=depth,
                as_of=ts,
                acl_subject=acting_on_behalf_of,
                allowed_acls=allowed_acls,
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
            policy_decisions=policy_decisions,
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
        allowed_acls, policy_decisions = await _resolve_allowed_acls(acting_on_behalf_of)
        facts = await store.facts_for_entity(
            entity_id,
            as_of=ts,
            acl_subject=acting_on_behalf_of,
            allowed_acls=allowed_acls,
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
            policy_decisions=policy_decisions,
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

    @mcp.tool()
    async def ask(
        question: str,
        agent_identity: str,
        acting_on_behalf_of: str | None = None,
    ) -> ToolResponse:
        """Ask a natural-language question. Runs planner → executor →
        ranked-with-provenance results.

        Requires a Planner to be configured on the server (M3.a). Without
        one this tool returns a clear error rather than degrading to a
        raw store call — a silent downgrade would violate the
        schema-constrained contract.
        """
        started = time.perf_counter()
        if resolved_planner is None:
            record = _emit(
                tool="ask",
                agent_identity=agent_identity,
                acting_on_behalf_of=acting_on_behalf_of,
                query_text=question,
                tool_arguments={},
                result_fact_ids=[],
                latency_ms=(time.perf_counter() - started) * 1000,
                policy_decisions=[{"policy": "planner", "result": "not_configured"}],
            )
            return ToolResponse(
                query_id=record.query_id,
                audit_hash=record.hash,
                payload={"error": "no planner configured on this runtime"},
            )

        # Ontology is required to build a Plan. If none is loaded, we
        # can't run the planner. Same fail-loud posture.
        if cfg.ontology_path is None:
            record = _emit(
                tool="ask",
                agent_identity=agent_identity,
                acting_on_behalf_of=acting_on_behalf_of,
                query_text=question,
                tool_arguments={},
                result_fact_ids=[],
                latency_ms=(time.perf_counter() - started) * 1000,
                policy_decisions=[{"policy": "ontology", "result": "not_configured"}],
            )
            return ToolResponse(
                query_id=record.query_id,
                audit_hash=record.hash,
                payload={"error": "no ontology configured on this runtime"},
            )

        # Deferred import so `ontos.ontology` isn't loaded on the hot path
        # for the read tools that don't need it.
        from ontos.ontology import load_ontology

        ontology = load_ontology(cfg.ontology_path)
        allowed_acls, policy_decisions = await _resolve_allowed_acls(acting_on_behalf_of)

        try:
            plan = await resolved_planner.plan(question, ontology)
        except PlannerError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            record = _emit(
                tool="ask",
                agent_identity=agent_identity,
                acting_on_behalf_of=acting_on_behalf_of,
                query_text=question,
                tool_arguments={"planner_id": resolved_planner.id},
                result_fact_ids=[],
                latency_ms=latency_ms,
                policy_decisions=[
                    *policy_decisions,
                    {"policy": "planner", "result": f"error:{exc}"},
                ],
            )
            return ToolResponse(
                query_id=record.query_id,
                audit_hash=record.hash,
                payload={"error": str(exc)},
            )

        execution = await resolved_executor.execute(
            plan,
            store,
            acl_subject=acting_on_behalf_of,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="ask",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=question,
            tool_arguments={
                "planner_id": resolved_planner.id,
                "executor_id": resolved_executor.id,
                "plan": plan.model_dump(mode="json"),
            },
            result_fact_ids=[str(h.fact.id) for h in execution.hits],
            latency_ms=latency_ms,
            policy_decisions=[
                *policy_decisions,
                {
                    "policy": "executor",
                    "executor_id": resolved_executor.id,
                    "hits": str(len(execution.hits)),
                    "warnings": ",".join(execution.warnings),
                },
            ],
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload={
                "plan": plan.model_dump(mode="json"),
                "hits": [h.model_dump(mode="json") for h in execution.hits],
                "warnings": execution.warnings,
            },
        )

    return mcp


def _default_dev_signing_key_if_missing() -> None:
    """Default a dev signing key when running outside prod.

    Factored so both the live-serve (`main`) and snapshot-serve
    (`ontos.cli.main.serve_snapshot`) paths share the same guard.
    """
    if os.environ.get("ONTOS_ENV") != "prod" and not os.environ.get("ONTOS_AUDIT_SIGNING_KEY"):
        os.environ["ONTOS_AUDIT_SIGNING_KEY"] = "dev-only-signing-key-do-not-use"
        log.warning("using dev signing key — never do this in prod")


def run_server(server: FastMCP, *, host: str, port: int, mode: str = "live") -> None:
    """Boot a built MCP server with streamable-HTTP transport.

    `mode` is logged for operator orientation — `"live"` for the
    standard `ontos serve` path, `"snapshot"` when serving a frozen
    pickle via `ontos serve --snapshot`. Behavior is identical;
    the string just lands in the structured log line so a dashboard
    can slice by it.
    """
    log.info("ontos starting", host=host, port=port, mode=mode)
    server.run(transport="http", host=host, port=port)


def main() -> None:
    """CLI entry — boots the live server with streamable-HTTP transport."""
    _default_dev_signing_key_if_missing()
    cfg = RuntimeConfig.from_env()
    server = build_server(cfg)
    log.info("backend", backend=cfg.storage_backend)
    run_server(server, host=cfg.listen_host, port=cfg.listen_port, mode="live")


if __name__ == "__main__":
    main()
