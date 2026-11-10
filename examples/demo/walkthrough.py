"""Engineering walkthrough: validity windows, permissions and audit over MCP.

Deterministic, no LLM: facts are written directly with explicit validity
dates, then read back through the real MCP tools (in-process), as two
different users. Shows:

  1. Bitemporal reads - the same question `as_of` two dates gives two
     answers, each with its source; history is closed, never overwritten.
  2. Permission pruning - a restricted fact is invisible to a user without
     access, and `provenance` on it returns exactly what a nonexistent id
     returns (no existence leak).
  3. One Article-12 audit record per call, hash-chained and verifiable.

    uv run python examples/demo/walkthrough.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.authz import VIEW, InMemoryAuthz
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import ArticleTwelveField, Confidence, Entity, Fact, Provenance
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore

JAN_2024 = datetime(2024, 1, 15, tzinfo=UTC)
MAR_2026 = datetime(2026, 3, 1, tzinfo=UTC)
MID_2025 = "2025-06-30T00:00:00Z"
TODAY = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provenance(source: str) -> Provenance:
    return Provenance(
        source_id=source,
        extractor_id="walkthrough",
        extractor_version="1",
        ontology_id="ontos.starter",
        ontology_version="0.1",
        confidence=Confidence.EXTRACTED,
        confidence_score=0.95,
    )


def _fact(
    subject: str, predicate: str, obj: str, source: str, valid_from: datetime, **kw: object
) -> Fact:
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=_provenance(source),
        t_valid=valid_from,
        ingested_at=valid_from,
        **kw,  # type: ignore[arg-type]
    )


async def _build_store() -> tuple[NetworkxStore, Fact]:
    store = NetworkxStore()
    for entity_id, entity_type, name in [
        ("Person:dana lee", "Person", "Dana Lee"),
        ("Person:sam ortiz", "Person", "Sam Ortiz"),
        ("Company:acme corp", "Company", "Acme Corp"),
        ("Company:beta labs", "Company", "Beta Labs"),
    ]:
        await store.upsert_entity(
            Entity(
                id=entity_id, type=entity_type, canonical_name=name, provenance=_provenance("seed")
            )
        )

    dana = _fact("Person:dana lee", "works_at", "Company:acme corp", "2024-team.md", JAN_2024)
    await store.add_fact(dana)
    # March 2026: Sam replaces Dana. Dana's fact is closed, not overwritten.
    sam = _fact("Person:sam ortiz", "works_at", "Company:acme corp", "2026-update.md", MAR_2026)
    await store.add_fact(sam)
    await store.close_fact(dana.id, t_invalid=MAR_2026, superseded_by=sam.id)

    # A Finance-only fact: only users with VIEW on acl:finance may see it.
    secret = _fact(
        "Company:acme corp",
        "acquired",
        "Company:beta labs",
        "board-minutes.md",
        MAR_2026,
        acl_ref="acl:finance",
    )
    await store.add_fact(secret)
    return store, secret


def _show(title: str, payload: list[dict] | dict | None) -> None:
    print(f"  {title}")
    facts = payload if isinstance(payload, list) else ([payload] if payload else [])
    if not facts:
        print("    (nothing)")
    for f in facts:
        window = f"valid {f['t_valid'][:10]} -> {(f.get('t_invalid') or 'now')[:10]}"
        print(
            f"    {f['subject_id']} -{f['predicate']}-> {f['object_id']}"
            f"   [source: {f['provenance']['source_id']}, {window}]"
        )


async def main() -> None:
    os.environ.setdefault("ONTOS_AUDIT_SIGNING_KEY", "walkthrough-key")
    store, secret = await _build_store()
    authz = InMemoryAuthz()
    authz.grant("user:finance-analyst", VIEW, "acl:finance")
    audit = AuditEmitter(signing_key=b"walkthrough-key")
    server = build_server(RuntimeConfig.from_env(), store=store, audit=audit, authz=authz)

    analyst = {"agent_identity": "agent:copilot", "acting_on_behalf_of": "user:finance-analyst"}
    intern = {"agent_identity": "agent:copilot", "acting_on_behalf_of": "user:intern"}

    async with Client(server) as mcp:
        print("\n== 1. Same question, two dates (bitemporal)")
        for label, as_of in (("as of 2025-06-30", MID_2025), ("as of today", TODAY)):
            r = await mcp.call_tool(
                "explain", {"entity_id": "Company:acme corp", "as_of": as_of, **intern}
            )
            _show(
                f"Who works at Acme Corp, {label}:",
                [f for f in r.data.payload if f["predicate"] == "works_at"],
            )
        history = await mcp.call_tool(
            "provenance",
            {
                "fact_id": str(
                    (await store.facts_for_entity("Person:dana lee", as_of=JAN_2024))[0].id
                ),
                **intern,
            },
        )
        _show("Dana's fact is closed, not deleted (superseded by Sam's):", history.data.payload)

        print("\n== 2. Permissions: pruned during traversal, no existence leak")
        for who, caller in (("finance analyst", analyst), ("intern", intern)):
            r = await mcp.call_tool("explain", {"entity_id": "Company:acme corp", **caller})
            _show(f"explain(Acme Corp) as the {who}:", r.data.payload)
        hidden = await mcp.call_tool("provenance", {"fact_id": str(secret.id), **intern})
        missing = await mcp.call_tool("provenance", {"fact_id": str(uuid4()), **intern})
        print(f"  intern asks for the Finance fact by id   -> {hidden.data.payload}")
        print(f"  intern asks for an id that doesn't exist -> {missing.data.payload}")
        print("  identical responses: the intern can't tell the hidden fact exists")

        print("\n== 3. Audit: one Article-12 record per call, hash-chained")
        last = audit._chain[-1].article12
        for key in (
            "tool_invoked",
            "agent_identity",
            "acting_on_behalf_of",
            "result_fact_ids",
            "timestamp",
        ):
            print(f"    {key}: {last[key]}")
        decisions = json.loads(last[ArticleTwelveField.POLICY_DECISIONS])
        print(f"    policy_decisions: {decisions}")
        print(f"  {len(audit)} records written; chain verifies: {audit.verify_chain()}")


if __name__ == "__main__":
    asyncio.run(main())
