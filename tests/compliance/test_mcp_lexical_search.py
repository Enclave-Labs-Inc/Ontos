"""#119 compliance: the MCP `search` tool's lexical mode is audited and ACL-safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ontos.audit.emitter import AuditEmitter
from ontos.authz import VIEW, InMemoryAuthz
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fact(n: int, subject: str, obj: str, acl_ref: str | None = None) -> Fact:
    return Fact(
        id=UUID(int=n),
        subject_id=subject,
        predicate="works_at",
        object_id=obj,
        provenance=Provenance(
            source_id="doc.md",
            extractor_id="test",
            extractor_version="1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=T0,
        ingested_at=T0,
        acl_ref=acl_ref,
    )


async def _server(audit: AuditEmitter):
    store = NetworkxStore()
    await store.add_fact(_fact(1, "Person:dana", "Company:acme"))
    await store.add_fact(_fact(2, "Person:secret", "Company:acme", acl_ref="acl:hr"))
    authz = InMemoryAuthz()
    authz.grant("user:hr", VIEW, "acl:hr")
    return build_server(RuntimeConfig.from_env(), store=store, audit=audit, authz=authz)


def _arguments(audit: AuditEmitter) -> dict[str, object]:
    record = audit._chain[-1]
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "search"
    return json.loads(record.article12[ArticleTwelveField.TOOL_ARGUMENTS])


@pytest.mark.asyncio
@pytest.mark.parametrize(("principal", "expected"), [("user:intern", 1), ("user:hr", 2)])
async def test_lexical_mode_is_acl_filtered_and_audited(principal: str, expected: int) -> None:
    audit = AuditEmitter(signing_key=b"mcp-lexical")
    async with Client(await _server(audit)) as client:
        result = await client.call_tool(
            "search",
            {
                "query": "who works at acme",
                "mode": "lexical",
                "agent_identity": "agent",
                "acting_on_behalf_of": principal,
            },
        )

    assert len(result.structured_content["payload"]) == expected
    arguments = _arguments(audit)
    assert arguments["mode"] == "lexical"
    # Never counts or scores of filtered items.
    assert set(arguments) == {"as_of", "k", "mode"}


@pytest.mark.asyncio
async def test_default_mode_is_substring_and_audited() -> None:
    audit = AuditEmitter(signing_key=b"mcp-lexical")
    async with Client(await _server(audit)) as client:
        await client.call_tool(
            "search", {"query": "acme", "agent_identity": "agent", "acting_on_behalf_of": "user:x"}
        )

    assert _arguments(audit)["mode"] == "substring"


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected_and_audited() -> None:
    audit = AuditEmitter(signing_key=b"mcp-lexical")
    async with Client(await _server(audit)) as client:
        with pytest.raises(ToolError, match="mode must be one of"):
            await client.call_tool(
                "search",
                {
                    "query": "acme",
                    "mode": "vector",
                    "agent_identity": "agent",
                    "acting_on_behalf_of": "user:x",
                },
            )

    record = audit._chain[-1]
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "search"
    assert record.article12[ArticleTwelveField.RESULT_FACT_IDS] in ("[]", [])
