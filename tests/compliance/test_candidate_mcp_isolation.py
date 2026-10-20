"""Candidate inspection must not be exposed through unauthenticated MCP."""

from __future__ import annotations

import pytest
from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.server import build_server
from ontos.storage.networkx_store import NetworkxStore


async def test_unauthed_mcp_does_not_expose_candidates() -> None:
    store = NetworkxStore()

    await store.add_candidates([
        RejectedCandidate(
            raw_triple=CandidateTriple(
                subject_id="private-person",
                subject_type="Person",
                subject_canonical_name="Private Person",
                predicate="unapproved_relationship",
                object_id="private-company",
                object_type="Company",
                object_canonical_name="Private Company",
                llm_confidence=0.9,
            ),
            reason="pattern_not_in_ontology",
            provenance=CandidateProvenance(
                source_id="private-document",
                extractor_id="test",
                extractor_version="1",
                ontology_id="starter",
                ontology_version="1",
                confidence_score=0.9,
            ),
            acl_ref="acl:private",
        )
    ])

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:private")

    audit = AuditEmitter(signing_key=b"mcp-isolation-test")
    server = build_server(
        RuntimeConfig.from_env(),
        store=store,
        audit=audit,
        authz=authz,
    )

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}

        assert "candidates" not in names
        assert not any(name.startswith("ontology_candidates") for name in names)
