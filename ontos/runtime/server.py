"""FastMCP entry point.

Wires storage + audit + (later) planner/executor/authz behind the MCP tool surface.
Every tool call emits an Article-12 audit record before returning.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel
from starlette.middleware import Middleware

from ontos.audit.base import AuditSink
from ontos.audit.emitter import AuditEmitter
from ontos.authz import AUDIT_EXPORTER, VIEW, AuthzBackend, InMemoryAuthz
from ontos.executor import DeterministicExecutor, Executor
from ontos.planner import Planner, PlannerError
from ontos.runtime.auth import (
    AuthRejectionAuditMiddleware,
    CallerIdentity,
    CallerRejectedError,
    build_token_verifier,
    enforce_bind_policy,
    parse_auth_mode,
    resolve_caller,
)
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, AuditRecord, Fact
from ontos.runtime.temporal import parse_as_of
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


class SnapshotMetadata(BaseModel):
    """Load-time metadata for a `ontos serve --snapshot` session.

    Passed into `build_server(snapshot_metadata=...)` to register the
    `snapshot_info` MCP tool. Values are captured at load time (before
    any caller context exists), so the tool returns the SAME frozen
    values to every caller regardless of ACL — nothing on this struct
    is a per-caller view onto the graph.

    - `id` — stable short hash of the snapshot bytes; disambiguates
      multiple snapshots an orchestrator may have loaded.
    - `mtime` — ISO-8601 UTC mtime of the source file (operator
      orientation: "when was this snapshot taken").
    - `format` — snapshot format identifier (e.g. `"ontos-nx-store-v3"`
      today; new values land alongside #32's text formats).
    - `ontology_id` / `ontology_version` — the ontology stamp from
      #34 (all facts within one snapshot agree). Captured at load
      time from one fact under the loader's root context; does not
      re-read `_facts` on every tool call.

    No `fact_count`, no `path`. Both were present in the first draft
    and both leak: `fact_count` exposes the unfiltered total pre-ACL
    (CLAUDE.md: no counts, no markers), and the absolute path exposes
    the operator's filesystem layout.
    """

    id: str
    mtime: str
    format: str
    ontology_id: str
    ontology_version: str


async def initialize_store(store: object) -> None:
    """Run the backend's idempotent schema setup, if it has one.

    Neo4j creates its indexes, uniqueness constraints and the candidate
    migration in `initialize()`. Nothing on the runtime path called it, so
    production databases ran without them. Call this on the event loop
    that will use the store: the Neo4j async driver binds its pool to it.
    """
    initialize = getattr(store, "initialize", None)
    if initialize is not None:
        await initialize()


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

        uri = config.neo4j_uri
        user = config.neo4j_user
        password = config.neo4j_password
        if not uri or not password:
            raise RuntimeError("ONTOS_STORAGE_BACKEND=neo4j requires NEO4J_URI + NEO4J_PASSWORD")
        return Neo4jStore.from_uri(
            uri,
            auth=(user, password),
            database=config.neo4j_database,
        )
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
    snapshot_metadata: SnapshotMetadata | None = None,
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

    auth_mode = parse_auth_mode(cfg.auth_mode)

    @asynccontextmanager
    async def _lifespan(_: FastMCP[Any]) -> AsyncIterator[dict[str, Any]]:
        await initialize_store(store)
        yield {}

    mcp = FastMCP("ontos", auth=build_token_verifier(cfg), lifespan=_lifespan)

    def _emit(
        *,
        tool: str,
        caller: CallerIdentity,
        query_text: str,
        tool_arguments: dict[str, object],
        result_fact_ids: list[str],
        latency_ms: float,
        policy_decisions: list[dict[str, str]],
    ) -> AuditRecord:
        return audit.emit(
            query_id=None,
            agent_identity=caller.agent_identity,
            acting_on_behalf_of=caller.principal,
            query_text=query_text,
            tool_invoked=tool,
            tool_arguments=tool_arguments,
            result_fact_ids=result_fact_ids,
            latency_ms=latency_ms,
            model_versions=_model_versions(cfg),
            policy_decisions=[caller.policy_decision(), *policy_decisions],
        )

    def _authenticate(
        *,
        tool: str,
        agent_identity: str,
        acting_on_behalf_of: str | None,
        query_text: str,
    ) -> CallerIdentity:
        """Bind this call to an identity before any store access.

        A rejection is audited first, then surfaced as a generic
        ToolError; the specific reason lives only in the audit record.
        """
        started = time.perf_counter()
        try:
            return resolve_caller(
                mode=auth_mode,
                claimed_agent_identity=agent_identity,
                claimed_principal=acting_on_behalf_of,
                token=get_access_token(),
            )
        except CallerRejectedError as exc:
            decision = {
                "policy": "authn",
                "method": auth_mode,
                "result": "deny",
                "reason": exc.reason,
            }
            if acting_on_behalf_of is not None:
                decision["claimed_principal"] = acting_on_behalf_of
            audit.emit(
                query_id=None,
                agent_identity=agent_identity,
                acting_on_behalf_of=None,
                query_text=query_text,
                tool_invoked=tool,
                tool_arguments={},
                result_fact_ids=[],
                latency_ms=(time.perf_counter() - started) * 1000,
                model_versions=_model_versions(cfg),
                policy_decisions=[decision],
            )
            raise ToolError("authentication failed") from None

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
        caller = _authenticate(
            tool="search",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query,
        )
        ts = parse_as_of(as_of) if as_of else None
        allowed_acls, policy_decisions = await _resolve_allowed_acls(caller.principal)
        facts = await store.search(
            query,
            as_of=ts,
            k=k,
            acl_subject=caller.principal,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="search",
            caller=caller,
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
        caller = _authenticate(
            tool="traverse",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=start,
        )
        ts = parse_as_of(as_of) if as_of else None
        allowed_acls, policy_decisions = await _resolve_allowed_acls(caller.principal)
        facts = list(
            await store.traverse(
                start,
                relation=relation,
                depth=depth,
                as_of=ts,
                acl_subject=caller.principal,
                allowed_acls=allowed_acls,
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="traverse",
            caller=caller,
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
        caller = _authenticate(
            tool="explain",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=entity_id,
        )
        ts = parse_as_of(as_of) if as_of else None
        allowed_acls, policy_decisions = await _resolve_allowed_acls(caller.principal)
        facts = await store.facts_for_entity(
            entity_id,
            as_of=ts,
            acl_subject=caller.principal,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="explain",
            caller=caller,
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
    async def provenance(
        fact_id: str,
        agent_identity: str,
        acting_on_behalf_of: str | None = None,
    ) -> ToolResponse:
        """Return the full provenance chain for one fact the caller may see.

        A fact outside the caller's ACLs returns the same `null` payload as
        a nonexistent id, so the response never reveals that it exists.
        """
        started = time.perf_counter()
        caller = _authenticate(
            tool="provenance",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=fact_id,
        )
        allowed_acls, policy_decisions = await _resolve_allowed_acls(caller.principal)
        fact = await store.get_visible_fact(
            UUID(fact_id),
            acl_subject=caller.principal,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        payload: Fact | None = fact
        record = _emit(
            tool="provenance",
            caller=caller,
            query_text=fact_id,
            tool_arguments={},
            result_fact_ids=[fact_id] if fact else [],
            latency_ms=latency_ms,
            policy_decisions=policy_decisions,
        )
        return ToolResponse(
            query_id=record.query_id,
            audit_hash=record.hash,
            payload=payload.model_dump(mode="json") if payload else None,
        )

    @mcp.tool()
    async def audit_lookup(
        query_id: str,
        agent_identity: str,
        acting_on_behalf_of: str | None = None,
    ) -> ToolResponse:
        """Return the Article-12 audit record for a prior query.

        Callers may read records of their own queries. Reading another
        principal's records requires `audit_exporter` on `audit`. A record
        the caller may not read returns the same `null` payload as a
        nonexistent one.
        """
        started = time.perf_counter()
        caller = _authenticate(
            tool="audit",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query_id,
        )
        record = audit.by_query_id(UUID(query_id))
        readable = False
        if record is not None and caller.principal is not None:
            owner = record.article12.get(ArticleTwelveField.ACTING_ON_BEHALF_OF)
            readable = owner == caller.principal or (
                resolved_authz is not None
                and await resolved_authz.check(caller.principal, AUDIT_EXPORTER, "audit")
            )
        emitted = _emit(
            tool="audit",
            caller=caller,
            query_text=query_id,
            tool_arguments={},
            result_fact_ids=[],
            latency_ms=(time.perf_counter() - started) * 1000,
            # One outcome for "missing" and "not yours": the caller can read
            # this record back, so distinct values would reveal existence.
            policy_decisions=[
                {"policy": "audit_lookup", "result": "returned" if readable else "none"}
            ],
        )
        return ToolResponse(
            query_id=emitted.query_id,
            audit_hash=emitted.hash,
            payload=record.model_dump(mode="json") if record is not None and readable else None,
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
        caller = _authenticate(
            tool="ask",
            agent_identity=agent_identity,
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=question,
        )
        if resolved_planner is None:
            record = _emit(
                tool="ask",
                caller=caller,
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
                caller=caller,
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
        allowed_acls, policy_decisions = await _resolve_allowed_acls(caller.principal)

        try:
            plan = await resolved_planner.plan(question, ontology)
        except PlannerError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            record = _emit(
                tool="ask",
                caller=caller,
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
            acl_subject=caller.principal,
            allowed_acls=allowed_acls,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        record = _emit(
            tool="ask",
            caller=caller,
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

    if snapshot_metadata is not None:
        # #33 PR #53 review: register snapshot_info INSIDE build_server
        # so it threads through the same `_emit` closure every other
        # tool uses. Pre-review the tool was registered at the CLI shim
        # layer and bypassed the audit path entirely — a CLAUDE.md
        # violation. All values are captured at load time (frozen on
        # SnapshotMetadata), so the tool returns the same bytes to
        # every caller regardless of ACL.
        metadata_payload = snapshot_metadata.model_dump(mode="json")

        @mcp.tool()
        async def snapshot_info(agent_identity: str) -> ToolResponse:
            """Load-time metadata about the frozen snapshot bound to this server.

            Returns `{id, mtime, format, ontology_id, ontology_version}` —
            no fact count (would leak the unfiltered total pre-ACL) and
            no absolute path (would leak server filesystem layout).
            """
            started = time.perf_counter()
            caller = _authenticate(
                tool="snapshot_info",
                agent_identity=agent_identity,
                acting_on_behalf_of=None,
                query_text="",
            )
            latency_ms = (time.perf_counter() - started) * 1000
            record = _emit(
                tool="snapshot_info",
                caller=caller,
                query_text="",
                tool_arguments={},
                result_fact_ids=[],
                latency_ms=latency_ms,
                policy_decisions=[],
            )
            return ToolResponse(
                query_id=record.query_id,
                audit_hash=record.hash,
                payload=metadata_payload,
            )

    return mcp


def _default_dev_signing_key_if_missing() -> None:
    """Default a dev signing key when running outside prod.

    Factored so both the live-serve (`main`) and snapshot-serve
    (`ontos.cli.main._serve_snapshot`) paths share the same guard.
    """
    if os.environ.get("ONTOS_ENV") != "prod" and not os.environ.get("ONTOS_AUDIT_SIGNING_KEY"):
        os.environ["ONTOS_AUDIT_SIGNING_KEY"] = "dev-only-signing-key-do-not-use"
        log.warning("using dev signing key — never do this in prod")


def _model_versions(config: RuntimeConfig) -> dict[str, str]:
    return {"runtime": "0.0.1", "backend": config.storage_backend}


def run_server(
    server: FastMCP,
    *,
    host: str,
    port: int,
    mode: str = "live",
    backend: str = "networkx",
    auth_mode: str = "none",
    allow_unauthenticated_bind: bool = False,
    audit: AuditSink | None = None,
) -> None:
    """Boot a built MCP server with streamable-HTTP transport.

    `mode` is `"live"` for the standard `ontos serve` path, `"snapshot"`
    when serving a frozen pickle via `ontos serve --snapshot`. Behavior
    is identical; the string lands in the structured log line so a
    dashboard can slice by it. `backend` reports the storage backend
    name on the same startup line — kept as a kwarg so dashboards that
    previously parsed `backend=…` out of the live-serve startup record
    continue to work.

    Refuses a non-loopback bind without authentication (#69). With
    `auth_mode="jwt"` and an `audit` sink, requests the transport
    rejects (401/403) are audited too.
    """
    enforce_bind_policy(
        auth_mode=auth_mode,
        host=host,
        allow_unauthenticated_bind=allow_unauthenticated_bind,
    )
    middleware: list[Middleware] = []
    if audit is not None and parse_auth_mode(auth_mode) != "none":
        middleware.append(
            Middleware(
                AuthRejectionAuditMiddleware,
                audit=audit,
                model_versions={"runtime": "0.0.1", "backend": backend},
            )
        )
    log.info("ontos starting", host=host, port=port, mode=mode, backend=backend, auth=auth_mode)
    server.run(transport="http", host=host, port=port, middleware=middleware or None)


def main(config: RuntimeConfig | None = None) -> None:
    """CLI entry — boots the live server with streamable-HTTP transport."""
    _default_dev_signing_key_if_missing()
    cfg = config or RuntimeConfig.from_env()
    audit = AuditEmitter.from_env(cfg.audit_signing_key_env)
    server = build_server(cfg, audit=audit)
    run_server(
        server,
        host=cfg.listen_host,
        port=cfg.listen_port,
        mode="live",
        backend=cfg.storage_backend,
        auth_mode=cfg.auth_mode,
        allow_unauthenticated_bind=cfg.allow_unauthenticated_bind,
        audit=audit,
    )


if __name__ == "__main__":
    main()
