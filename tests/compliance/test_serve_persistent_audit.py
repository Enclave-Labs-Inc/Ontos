"""Gate-blocking: `ontos serve` audit survives a restart (#93).

The server built an in-memory `AuditEmitter` and never read
`ONTOS_AUDIT_DB_URL`, so every Article-12 record was lost when the pod
restarted. `PostgresAuditEmitter` is async-only, so the request path has
to await it, and a sink failure must fail the call rather than return
an unaudited result.

The SQLite (aiosqlite) URL runs the same `PostgresAuditEmitter` code
path without a container; `tests/integration/` covers real Postgres.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.types import Message, Receive, Scope, Send

from ontos import __version__
from ontos.audit.emitter import AuditEmitter
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.runtime.auth import AuthRejectionAuditMiddleware
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.server import build_audit_sink, build_server
from ontos.storage.networkx_store import NetworkxStore

_KEY_ENV = "ONTOS_TEST_PERSISTENT_AUDIT_KEY"


@pytest.fixture
def persistent_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    monkeypatch.setenv(_KEY_ENV, "persistent-audit-key")
    return replace(
        RuntimeConfig.from_env(),
        audit_db_url=f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}",
        audit_signing_key_env=_KEY_ENV,
        audit_persistent=True,
    )


def test_audit_db_url_selects_the_persistent_sink(persistent_cfg: RuntimeConfig) -> None:
    assert isinstance(build_audit_sink(persistent_cfg), PostgresAuditEmitter)


def test_audit_db_url_env_marks_config_persistent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_AUDIT_DB_URL", raising=False)
    assert RuntimeConfig.from_env().audit_persistent is False
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", "postgresql+asyncpg://h/db")
    assert RuntimeConfig.from_env().audit_persistent is True


def test_dev_without_audit_db_url_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_ENV", raising=False)
    monkeypatch.setenv(_KEY_ENV, "k")
    cfg = replace(RuntimeConfig.from_env(), audit_signing_key_env=_KEY_ENV, audit_persistent=False)
    assert isinstance(build_audit_sink(cfg), AuditEmitter)


def test_prod_refuses_to_boot_without_persistent_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOS_ENV", "prod")
    monkeypatch.setenv(_KEY_ENV, "k")
    cfg = replace(RuntimeConfig.from_env(), audit_signing_key_env=_KEY_ENV, audit_persistent=False)
    with pytest.raises(RuntimeError, match="ONTOS_AUDIT_DB_URL"):
        build_audit_sink(cfg)


async def test_tool_records_survive_a_restart(persistent_cfg: RuntimeConfig) -> None:
    audit = build_audit_sink(persistent_cfg)
    server = build_server(persistent_cfg, store=NetworkxStore(), audit=audit)

    async with Client(server) as client:
        searched = await client.call_tool("search", {"query": "x", "agent_identity": "agent-a"})
        traversed = await client.call_tool("traverse", {"start": "x", "agent_identity": "agent-a"})
    query_ids = [searched.data.query_id, traversed.data.query_id]

    restarted = build_audit_sink(persistent_cfg)
    assert isinstance(restarted, PostgresAuditEmitter)
    try:
        assert await restarted.count_async() == 2
        assert await restarted.verify_chain_async()
        for query_id in query_ids:
            record = await restarted.by_query_id_async(query_id)
            assert record is not None
            versions = json.loads(record.article12[ArticleTwelveField.MODEL_VERSIONS])
            assert versions["runtime"] == __version__
    finally:
        await restarted.close()


async def test_audit_tool_reads_back_from_the_persistent_sink(
    persistent_cfg: RuntimeConfig,
) -> None:
    server = build_server(
        persistent_cfg, store=NetworkxStore(), audit=build_audit_sink(persistent_cfg)
    )

    async with Client(server) as client:
        searched = await client.call_tool(
            "search", {"query": "x", "agent_identity": "a", "acting_on_behalf_of": "user:a"}
        )
        looked_up = await client.call_tool(
            "audit_lookup",
            {
                "query_id": str(searched.data.query_id),
                "agent_identity": "a",
                "acting_on_behalf_of": "user:a",
            },
        )

    payload: Any = looked_up.data.payload
    assert payload is not None
    assert payload["query_id"] == str(searched.data.query_id)


async def test_a_failing_sink_fails_the_call_without_returning_data(
    persistent_cfg: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = build_audit_sink(persistent_cfg)
    server = build_server(persistent_cfg, store=NetworkxStore(), audit=audit)

    async def down(**_: object) -> None:
        raise ConnectionError("SECRET-DSN postgres://ontos:hunter2@db:5432")

    monkeypatch.setattr(audit, "emit_async", down)
    async with Client(server) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("search", {"query": "x", "agent_identity": "a"})

    assert str(raised.value) == "audit unavailable"


async def test_transport_rejection_is_persisted(persistent_cfg: RuntimeConfig) -> None:
    audit = build_audit_sink(persistent_cfg)
    assert isinstance(audit, PostgresAuditEmitter)
    await audit.initialize()

    async def unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = []

    async def capture(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    middleware = AuthRejectionAuditMiddleware(
        unauthorized, audit=audit, model_versions={"runtime": __version__}
    )
    await middleware(
        {"type": "http", "method": "POST", "path": "/mcp", "headers": []}, receive, capture
    )

    assert sent[0]["status"] == 401
    try:
        assert await audit.count_async() == 1
    finally:
        await audit.close()
