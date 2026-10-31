"""Gate-blocking: a visible fact must not expose an endpoint entity the
caller cannot see, on any read path.

Entities carry their own ACL. A public fact can point at an entity that a
restricted document later claimed (the #48 None -> X upgrade). `scan()` has
always hidden such facts, but search, traverse, explain and provenance only
checked the fact's ACL, so they returned the fact and with it the restricted
entity's id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.authz import VIEW, InMemoryAuthz
from ontos.executor import DeterministicExecutor
from ontos.ontology import load_ontology
from ontos.planner import Plan, SeedByEntity, SeedByKeyword, TraversalStep
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore

_PROV = Provenance(
    source_id="public-doc",
    extractor_id="test",
    extractor_version="1",
    confidence=Confidence.EXTRACTED,
    confidence_score=0.9,
)
_SECRET_ID = "secret-co"


async def _store_with_upgraded_endpoint() -> tuple[NetworkxStore, Fact]:
    store = NetworkxStore()
    await store.upsert_entity(Entity(id="p", type="Person", canonical_name="Pat", provenance=_PROV))
    fact = Fact(
        id=uuid4(),
        subject_id="p",
        predicate="works_at",
        object_id=_SECRET_ID,
        provenance=_PROV,
        t_valid=datetime(2025, 1, 1, tzinfo=UTC),
        ingested_at=datetime.now(UTC),
    )
    await store.add_fact(fact)  # public fact, raw public node for secret-co
    await store.upsert_entity(  # a restricted document claims it: None -> acl:hr
        Entity(id=_SECRET_ID, type="Company", canonical_name="Secret Co", provenance=_PROV),
        acl_ref="acl:hr",
    )
    return store, fact


def _authz() -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:hr", VIEW, "acl:hr")
    return authz


async def _call(client: Client, tool: str, args: dict[str, Any]) -> Any:
    result = await client.call_tool(tool, args)
    return result.structured_content["payload"]


@pytest.mark.parametrize("principal, sees_secret", [("user:outsider", False), ("user:hr", True)])
async def test_mcp_read_tools_hide_facts_with_invisible_endpoints(
    principal: str, sees_secret: bool
) -> None:
    store, fact = await _store_with_upgraded_endpoint()
    server = build_server(
        RuntimeConfig.from_env(),
        store=store,
        audit=AuditEmitter(signing_key=b"endpoint-acl"),
        authz=_authz(),
    )
    base = {"agent_identity": "agent", "acting_on_behalf_of": principal}

    async with Client(server) as client:
        payloads = {
            "search": await _call(client, "search", {**base, "query": "works_at"}),
            "traverse": await _call(client, "traverse", {**base, "start": "p"}),
            "explain(p)": await _call(client, "explain", {**base, "entity_id": "p"}),
            "explain(secret)": await _call(client, "explain", {**base, "entity_id": _SECRET_ID}),
            "provenance": await _call(client, "provenance", {**base, "fact_id": str(fact.id)}),
        }

    for name, payload in payloads.items():
        leaked = _SECRET_ID in json.dumps(payload)
        assert leaked is sees_secret, f"{name}: {payload!r}"


async def test_scan_and_read_paths_agree() -> None:
    store, _ = await _store_with_upgraded_endpoint()

    scan = await store.scan(allowed_acls=[])
    assert scan.facts == []
    assert await store.search("works_at", allowed_acls=[]) == []
    assert list(await store.traverse("p", allowed_acls=[])) == []
    assert await store.facts_for_entity("p", allowed_acls=[]) == []
    assert await store.search_entities("Pat", allowed_acls=[]) == []


async def test_executor_does_not_seed_or_expand_into_invisible_endpoints() -> None:
    store, _ = await _store_with_upgraded_endpoint()
    ontology = load_ontology(
        Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
    )
    step = TraversalStep(depth=1)
    for seed in (SeedByKeyword(keyword="works_at", k=5), SeedByEntity(entity_id="p")):
        plan = Plan.build(seed=seed, steps=[step], ontology=ontology)
        result = await DeterministicExecutor().execute(plan, store, allowed_acls=[])

        assert result.hits == [], seed
        assert _SECRET_ID not in " ".join(result.warnings), seed
