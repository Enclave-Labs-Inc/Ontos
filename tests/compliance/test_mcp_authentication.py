"""#69 — MCP callers are authenticated, not trusted.

Gate-blocking. With ``ONTOS_AUTH_MODE=jwt``:

- a request without a valid bearer token is rejected, and the rejection
  is audited even though no tool ran;
- a caller cannot read as another principal by passing a different
  ``acting_on_behalf_of``: the principal comes from the verified token;
- every registered tool refuses an unauthenticated call and audits it
  (the registry is read from the server, so new tools are covered
  automatically);
- with auth off, the server refuses a non-loopback bind.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from mcp.shared.exceptions import MCPError
from starlette.middleware import Middleware

from ontos.audit.emitter import AuditEmitter
from ontos.authz import VIEW, InMemoryAuthz
from ontos.runtime.auth import AuthRejectionAuditMiddleware, UnauthenticatedBindError
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, AuditRecord, Confidence, Fact, Provenance
from ontos.runtime.server import SnapshotMetadata, build_server, run_server
from ontos.storage.networkx_store import NetworkxStore

ISSUER = "https://idp.customer.internal"
AUDIENCE = "ontos"


@pytest.fixture
def key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def jwt_config(tmp_path: Path, key_pair: RSAKeyPair) -> RuntimeConfig:
    key_file = tmp_path / "idp.pem"
    key_file.write_text(key_pair.public_key)
    return dataclasses.replace(
        RuntimeConfig.from_env(),
        auth_mode="jwt",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_file=key_file,
    )


def _fact(subject: str, acl_ref: str) -> Fact:
    return Fact(
        id=uuid4(),
        subject_id=subject,
        predicate="acquired",
        object_id="Widget Inc",
        provenance=Provenance(
            source_id=f"doc:{acl_ref}",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.99,
        ),
        t_valid=datetime(2025, 1, 1, tzinfo=UTC),
        ingested_at=datetime.now(UTC),
        acl_ref=acl_ref,
    )


@pytest.fixture
async def finance_and_legal_store() -> tuple[NetworkxStore, Fact, Fact]:
    store = NetworkxStore()
    finance = _fact("Acme Finance", "acl:finance")
    legal = _fact("Acme Legal", "acl:legal")
    await store.add_fact(finance)
    await store.add_fact(legal)
    return store, finance, legal


@pytest.fixture
def authz() -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:finance")
    authz.grant("user:bob", VIEW, "acl:legal")
    return authz


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.asynccontextmanager
async def _serving(server: FastMCP, audit: AuditEmitter) -> AsyncIterator[str]:
    """Run `server` over real streamable HTTP, wired like `run_server`."""
    app = server.http_app(
        middleware=[
            Middleware(
                AuthRejectionAuditMiddleware,
                audit=audit,
                model_versions={"runtime": "test"},
            )
        ]
    )
    port = _free_port()
    uvi = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(uvi.serve())
    while not uvi.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvi.should_exit = True
        await task


def _records(audit: AuditEmitter) -> list[AuditRecord]:
    return list(audit._chain)


def _payload(result: Any) -> dict[str, Any]:
    content = result.structured_content
    assert isinstance(content, dict)
    return content


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}


@pytest.mark.parametrize("bearer", [None, "not-a-jwt"])
async def test_transport_rejects_and_audits_missing_or_invalid_token(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
    bearer: str | None,
) -> None:
    store, _, _ = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)
    headers = {"Accept": "application/json, text/event-stream"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"

    async with _serving(server, audit) as url, httpx.AsyncClient() as http:
        response = await http.post(url, json=_INITIALIZE, headers=headers)

    assert response.status_code == 401
    records = _records(audit)
    assert len(records) == 1
    article12 = records[0].article12
    for field in ArticleTwelveField:
        assert field in article12
    assert article12[ArticleTwelveField.TOOL_INVOKED] == "authn"
    decisions = article12[ArticleTwelveField.POLICY_DECISIONS]
    assert '"result": "deny"' in decisions
    expected_reason = "missing_token" if bearer is None else "invalid_token"
    assert f'"reason": "{expected_reason}"' in decisions
    assert "not-a-jwt" not in str(article12)


async def test_token_from_wrong_key_is_rejected(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, _, _ = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)
    forged = RSAKeyPair.generate().create_token(
        subject="user:bob", issuer=ISSUER, audience=AUDIENCE
    )

    async with _serving(server, audit) as url:
        with pytest.raises(MCPError):
            async with Client(url, auth=forged) as client:
                await client.call_tool("search", {"query": "Acme", "agent_identity": "a"})

    # Every attempt died at the transport: rejections are on record and
    # no tool ever ran as the forged subject.
    tools_invoked = {r.article12[ArticleTwelveField.TOOL_INVOKED] for r in _records(audit)}
    assert tools_invoked == {"authn"}


async def test_principal_comes_from_verified_token(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    key_pair: RSAKeyPair,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, finance, _ = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)
    token = key_pair.create_token(
        subject="user:alice",
        issuer=ISSUER,
        audience=AUDIENCE,
        additional_claims={"act": {"sub": "agent:copilot"}},
    )

    async with _serving(server, audit) as url, Client(url, auth=token) as client:
        result = await client.call_tool(
            "search", {"query": "Acme", "agent_identity": "agent:copilot"}
        )

    payload = _payload(result)
    assert [fact["id"] for fact in payload["payload"]] == [str(finance.id)]
    record = audit.by_query_id(UUID(payload["query_id"]))
    assert record is not None
    assert record.article12[ArticleTwelveField.ACTING_ON_BEHALF_OF] == "user:alice"
    assert record.article12[ArticleTwelveField.AGENT_IDENTITY] == "agent:copilot"
    decisions = record.article12[ArticleTwelveField.POLICY_DECISIONS]
    assert '"policy": "authn"' in decisions
    assert '"method": "jwt"' in decisions
    assert f'"issuer": "{ISSUER}"' in decisions


async def test_caller_cannot_claim_another_principal(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    key_pair: RSAKeyPair,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, _, legal = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)
    alice = key_pair.create_token(subject="user:alice", issuer=ISSUER, audience=AUDIENCE)

    async with _serving(server, audit) as url, Client(url, auth=alice) as client:
        with pytest.raises(ToolError, match="authentication failed") as exc:
            await client.call_tool(
                "search",
                {"query": "Acme", "agent_identity": "a", "acting_on_behalf_of": "user:bob"},
            )

    assert str(legal.id) not in str(exc.value)
    deny = _records(audit)[-1].article12
    assert deny[ArticleTwelveField.TOOL_INVOKED] == "search"
    assert deny[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
    decisions = deny[ArticleTwelveField.POLICY_DECISIONS]
    assert '"reason": "principal_mismatch"' in decisions
    assert '"claimed_principal": "user:bob"' in decisions


async def _registered_tools(server: FastMCP) -> list[Any]:
    async with Client(server) as client:
        return list(await client.list_tools())


def _minimal_arguments(tool: Any) -> dict[str, object]:
    schema = tool.input_schema
    arguments: dict[str, object] = {}
    for name in schema.get("required", []):
        kind = schema["properties"][name].get("type")
        arguments[name] = 1 if kind == "integer" else "x"
    return arguments


async def test_every_tool_refuses_and_audits_unauthenticated_calls(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, _, _ = finance_and_legal_store
    server = build_server(
        jwt_config,
        store=store,
        audit=audit,
        authz=authz,
        snapshot_metadata=SnapshotMetadata(
            id="0" * 16, mtime="", format="test", ontology_id="", ontology_version=""
        ),
    )
    tools = await _registered_tools(server)
    assert {"search", "traverse", "explain", "provenance", "ask", "snapshot_info"} <= {
        t.name for t in tools
    }

    # In-process transport carries no bearer token: each tool must refuse
    # on its own, independent of the HTTP layer.
    async with Client(server) as client:
        for tool in tools:
            before = len(audit)
            with pytest.raises(ToolError, match="authentication failed"):
                await client.call_tool(tool.name, _minimal_arguments(tool))
            assert len(audit) == before + 1, f"{tool.name} did not audit its rejection"
            record = _records(audit)[-1].article12
            for field in ArticleTwelveField:
                assert field in record
            assert record[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
            assert '"reason": "missing_token"' in record[ArticleTwelveField.POLICY_DECISIONS]


def test_unauthenticated_server_refuses_public_bind(audit: AuditEmitter) -> None:
    server = build_server(RuntimeConfig.from_env(), store=NetworkxStore(), audit=audit)

    with pytest.raises(UnauthenticatedBindError):
        run_server(server, host="0.0.0.0", port=_free_port(), auth_mode="none", audit=audit)
