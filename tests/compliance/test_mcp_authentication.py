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

from typing import Any
from uuid import UUID

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from mcp.shared.exceptions import MCPError

from ontos.audit.emitter import AuditEmitter
from ontos.authz import InMemoryAuthz
from ontos.runtime.auth import UnauthenticatedBindError
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Fact
from ontos.runtime.server import SnapshotMetadata, build_server, run_server
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance._mcp_harness import (
    AUDIENCE,
    ISSUER,
    audit_records,
    free_port,
    serving,
    structured_payload,
)

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

    async with serving(server, audit) as url, httpx.AsyncClient() as http:
        response = await http.post(url, json=_INITIALIZE, headers=headers)

    assert response.status_code == 401
    records = audit_records(audit)
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

    async with serving(server, audit) as url:
        with pytest.raises(MCPError):
            async with Client(url, auth=forged) as client:
                await client.call_tool("search", {"query": "Acme", "agent_identity": "a"})

    # Every attempt died at the transport: rejections are on record and
    # no tool ever ran as the forged subject.
    tools_invoked = {r.article12[ArticleTwelveField.TOOL_INVOKED] for r in audit_records(audit)}
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

    async with serving(server, audit) as url, Client(url, auth=token) as client:
        result = await client.call_tool(
            "search", {"query": "Acme", "agent_identity": "agent:copilot"}
        )

    payload = structured_payload(result)
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

    async with serving(server, audit) as url, Client(url, auth=alice) as client:
        with pytest.raises(ToolError, match="authentication failed") as exc:
            await client.call_tool(
                "search",
                {"query": "Acme", "agent_identity": "a", "acting_on_behalf_of": "user:bob"},
            )

    assert str(legal.id) not in str(exc.value)
    deny = audit_records(audit)[-1].article12
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
            record = audit_records(audit)[-1].article12
            for field in ArticleTwelveField:
                assert field in record
            assert record[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
            assert '"reason": "missing_token"' in record[ArticleTwelveField.POLICY_DECISIONS]


def test_unauthenticated_server_refuses_public_bind(audit: AuditEmitter) -> None:
    server = build_server(RuntimeConfig.from_env(), store=NetworkxStore(), audit=audit)

    with pytest.raises(UnauthenticatedBindError):
        run_server(server, host="0.0.0.0", port=free_port(), auth_mode="none", audit=audit)
