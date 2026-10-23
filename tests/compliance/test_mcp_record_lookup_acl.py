"""#78 — `provenance` and `audit_lookup` enforce read permissions.

Gate-blocking. Both tools fetch a record by id. They must not return a
fact outside the caller's ACLs or another principal's audit record, and
a forbidden record must be indistinguishable from a missing one, both in
the response and in the audit trail the caller can read back.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastmcp import Client
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from ontos.audit.emitter import AuditEmitter
from ontos.authz import InMemoryAuthz
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Fact
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance._mcp_harness import AUDIENCE, ISSUER, make_fact, serving, structured_payload


def _token(key_pair: RSAKeyPair, subject: str) -> str:
    return key_pair.create_token(subject=subject, issuer=ISSUER, audience=AUDIENCE)


def _decisions(audit: AuditEmitter, query_id: str) -> list[dict[str, str]]:
    record = audit.by_query_id(UUID(query_id))
    assert record is not None
    decisions: list[dict[str, str]] = json.loads(
        record.article12[ArticleTwelveField.POLICY_DECISIONS]
    )
    return decisions


async def test_forbidden_fact_is_indistinguishable_from_missing(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    key_pair: RSAKeyPair,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, finance, legal = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)

    async with (
        serving(server, audit) as url,
        Client(url, auth=_token(key_pair, "user:alice")) as alice,
    ):
        own = structured_payload(
            await alice.call_tool("provenance", {"fact_id": str(finance.id), "agent_identity": "a"})
        )
        forbidden = structured_payload(
            await alice.call_tool("provenance", {"fact_id": str(legal.id), "agent_identity": "a"})
        )
        missing = structured_payload(
            await alice.call_tool("provenance", {"fact_id": str(uuid4()), "agent_identity": "a"})
        )

    assert own["payload"]["id"] == str(finance.id)
    assert forbidden["payload"] is None
    assert missing["payload"] is None

    for response in (forbidden, missing):
        record = audit.by_query_id(UUID(response["query_id"]))
        assert record is not None
        assert record.article12[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
        assert record.article12[ArticleTwelveField.ACTING_ON_BEHALF_OF] == "user:alice"
    assert _decisions(audit, forbidden["query_id"]) == _decisions(audit, missing["query_id"])


async def test_audit_lookup_is_limited_to_own_records(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    key_pair: RSAKeyPair,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
    authz: InMemoryAuthz,
) -> None:
    store, _, _ = finance_and_legal_store
    server = build_server(jwt_config, store=store, audit=audit, authz=authz)

    async with serving(server, audit) as url:
        async with Client(url, auth=_token(key_pair, "user:bob")) as bob:
            bob_query = structured_payload(
                await bob.call_tool("search", {"query": "Acme", "agent_identity": "b"})
            )["query_id"]
            own = structured_payload(
                await bob.call_tool("audit_lookup", {"query_id": bob_query, "agent_identity": "b"})
            )

        async with Client(url, auth=_token(key_pair, "user:alice")) as alice:
            foreign = structured_payload(
                await alice.call_tool(
                    "audit_lookup", {"query_id": bob_query, "agent_identity": "a"}
                )
            )
            missing = structured_payload(
                await alice.call_tool(
                    "audit_lookup", {"query_id": str(uuid4()), "agent_identity": "a"}
                )
            )
            # Reading back the audit of those lookups must not tell
            # "someone else's" apart from "nonexistent" either.
            foreign_trace = structured_payload(
                await alice.call_tool(
                    "audit_lookup", {"query_id": foreign["query_id"], "agent_identity": "a"}
                )
            )
            missing_trace = structured_payload(
                await alice.call_tool(
                    "audit_lookup", {"query_id": missing["query_id"], "agent_identity": "a"}
                )
            )

    assert own["payload"]["query_id"] == bob_query
    assert foreign["payload"] is None
    assert missing["payload"] is None

    def decisions_of(trace: dict[str, object]) -> object:
        payload = trace["payload"]
        assert isinstance(payload, dict)
        return payload["article12"][ArticleTwelveField.POLICY_DECISIONS.value]

    assert decisions_of(foreign_trace) == decisions_of(missing_trace)


async def test_audit_exporter_can_read_other_principals_records(
    jwt_config: RuntimeConfig,
    audit: AuditEmitter,
    key_pair: RSAKeyPair,
    finance_and_legal_store: tuple[NetworkxStore, Fact, Fact],
) -> None:
    store, _, _ = finance_and_legal_store
    auditor_authz = InMemoryAuthz(audit_roles={"user:compliance"})
    server = build_server(jwt_config, store=store, audit=audit, authz=auditor_authz)

    async with serving(server, audit) as url:
        async with Client(url, auth=_token(key_pair, "user:bob")) as bob:
            bob_query = structured_payload(
                await bob.call_tool("search", {"query": "Acme", "agent_identity": "b"})
            )["query_id"]
        async with Client(url, auth=_token(key_pair, "user:compliance")) as auditor:
            seen = structured_payload(
                await auditor.call_tool(
                    "audit_lookup", {"query_id": bob_query, "agent_identity": "c"}
                )
            )

    assert seen["payload"]["query_id"] == bob_query


async def test_provenance_without_principal_only_returns_public_facts(
    audit: AuditEmitter,
) -> None:
    store = NetworkxStore()
    restricted = make_fact("Acme Restricted", "acl:finance")
    public = restricted.model_copy(update={"id": uuid4(), "acl_ref": None})
    await store.add_fact(restricted)
    await store.add_fact(public)
    server = build_server(RuntimeConfig.from_env(), store=store, audit=audit)

    async with Client(server) as client:
        shown = structured_payload(
            await client.call_tool("provenance", {"fact_id": str(public.id), "agent_identity": "a"})
        )
        hidden = structured_payload(
            await client.call_tool(
                "provenance", {"fact_id": str(restricted.id), "agent_identity": "a"}
            )
        )

    assert shown["payload"]["id"] == str(public.id)
    assert hidden["payload"] is None
