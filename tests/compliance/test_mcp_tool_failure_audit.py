"""Gate-blocking: every MCP tool audits its failures (audit-first).

A malformed argument, an authz backend outage or a store error used to
escape a tool after authentication with no Article-12 record. Each
failure must now leave exactly one record, and callers get a safe
message that never echoes backend errors.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ontos.audit.emitter import AuditEmitter
from ontos.authz import InMemoryAuthz
from ontos.authz.base import AuthzError
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.server import SnapshotMetadata, build_server
from ontos.storage.networkx_store import NetworkxStore

# Tools whose arguments have no format to violate. A new tool must either
# take a validated argument (and be exercised below) or be listed here.
_NO_VALIDATED_ARGUMENTS = {"ask", "snapshot_info"}
_MALFORMED = {
    "as_of": "not-a-timestamp",
    "fact_id": "not-a-uuid",
    "query_id": "not-a-uuid",
}


def _server(audit: AuditEmitter, **kwargs: Any):  # type: ignore[no-untyped-def]
    return build_server(
        RuntimeConfig.from_env(),
        audit=audit,
        snapshot_metadata=SnapshotMetadata(
            id="0" * 16, mtime="", format="test", ontology_id="", ontology_version=""
        ),
        **{"store": NetworkxStore(), **kwargs},
    )


def _decisions(audit: AuditEmitter) -> list[list[dict[str, str]]]:
    return [json.loads(r.article12[ArticleTwelveField.POLICY_DECISIONS]) for r in audit._chain]


def _arguments(tool: Any, **overrides: Any) -> dict[str, Any]:
    schema = tool.input_schema
    args: dict[str, Any] = {}
    for name in schema.get("required", []):
        args[name] = 1 if schema["properties"][name].get("type") == "integer" else "x"
    args.update(overrides)
    return args


async def test_every_tool_audits_malformed_arguments() -> None:
    audit = AuditEmitter(signing_key=b"failure-audit")
    server = _server(audit)

    async with Client(server) as client:
        tools = await client.list_tools()
        exercised = set()
        for tool in tools:
            props = tool.input_schema["properties"]
            bad = {name: value for name, value in _MALFORMED.items() if name in props}
            if not bad:
                assert tool.name in _NO_VALIDATED_ARGUMENTS, (
                    f"{tool.name} has no malformed-argument case; add one or list it"
                )
                continue
            for name, value in bad.items():
                before = len(audit)
                with pytest.raises(ToolError, match=name):
                    await client.call_tool(tool.name, _arguments(tool, **{name: value}))
                assert len(audit) == before + 1, f"{tool.name}/{name} not audited"
                assert _decisions(audit)[-1][-1] == {
                    "policy": "request",
                    "result": "invalid_request",
                }
                record = audit._chain[-1].article12
                assert record[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
                exercised.add(tool.name)

    assert {"search", "traverse", "explain", "provenance", "audit_lookup"} <= exercised
    assert audit.verify_chain()


@pytest.mark.parametrize(
    "tool, args",
    [
        ("search", {"query": "x", "k": 0}),
        ("search", {"query": "x", "k": 101}),
        ("traverse", {"start": "x", "depth": 0}),
        ("traverse", {"start": "x", "depth": 11}),
    ],
)
async def test_unbounded_k_and_depth_are_rejected_and_audited(
    tool: str, args: dict[str, Any]
) -> None:
    audit = AuditEmitter(signing_key=b"failure-audit")

    async with Client(_server(audit)) as client:
        with pytest.raises(ToolError, match="must be between"):
            await client.call_tool(tool, {"agent_identity": "a", **args})

    assert _decisions(audit)[-1][-1]["result"] == "invalid_request"


async def test_authz_backend_outage_is_audited_without_echoing_it() -> None:
    class DownAuthz(InMemoryAuthz):
        async def list_authorized_objects(self, subject: str, relation: str) -> list[str]:
            raise AuthzError("SECRET-INTERNAL fga.internal:8080 refused")

    audit = AuditEmitter(signing_key=b"failure-audit")
    async with Client(_server(audit, authz=DownAuthz())) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "search", {"query": "x", "agent_identity": "a", "acting_on_behalf_of": "user:a"}
            )

    assert "SECRET-INTERNAL" not in str(raised.value)
    assert _decisions(audit)[-1][-1] == {"policy": "request", "result": "authorization_error"}
    assert "SECRET-INTERNAL" not in json.dumps([dict(r.article12) for r in audit._chain])


async def test_store_failure_is_audited_without_echoing_it() -> None:
    class BrokenStore(NetworkxStore):
        async def search(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET-INTERNAL bolt://neo4j.internal password=hunter2")

    audit = AuditEmitter(signing_key=b"failure-audit")
    async with Client(_server(audit, store=BrokenStore())) as client:
        with pytest.raises(ToolError, match="internal error") as raised:
            await client.call_tool("search", {"query": "x", "agent_identity": "a"})

    assert "SECRET-INTERNAL" not in str(raised.value)
    assert _decisions(audit)[-1][-1] == {"policy": "request", "result": "error"}
    assert audit.verify_chain()
