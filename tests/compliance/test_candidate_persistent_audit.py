"""Verify candidate inspection records persist across audit connections."""

from __future__ import annotations

import pytest

from ontos.audit.postgres import PostgresAuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, InMemoryAuthz
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDeniedError,
    inspect_candidates,
)
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore


async def test_successful_inspection_persists_article12_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-only-key")
    url = f"sqlite+aiosqlite:///{tmp_path / 'candidate-audit.db'}"

    audit = PostgresAuditEmitter.from_url(url)
    await audit.initialize()

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")

    try:
        candidates, record = await inspect_candidates(
            candidate_store=NetworkxStore(),
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="ontos-cli",
            identity_assurance="operator_asserted",
        )
        assert candidates == []
        query_id = record.query_id
    finally:
        await audit.close()

    reopened = PostgresAuditEmitter.from_url(url)
    try:
        saved = await reopened.by_query_id_async(query_id)
        assert saved is not None
        for field in ArticleTwelveField:
            assert field in saved.article12
        assert saved.article12[ArticleTwelveField.TOOL_INVOKED] == "candidates"

        import json

        arguments = json.loads(saved.article12[ArticleTwelveField.TOOL_ARGUMENTS])
        assert arguments["identity_assurance"] == "operator_asserted"
        assert await reopened.verify_chain_async()
    finally:
        await reopened.close()


async def test_denied_inspection_persists_article12_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-only-key")
    url = f"sqlite+aiosqlite:///{tmp_path / 'candidate-denied.db'}"

    audit = PostgresAuditEmitter.from_url(url)
    await audit.initialize()

    try:
        with pytest.raises(CandidateInspectionDeniedError):
            await inspect_candidates(
                candidate_store=NetworkxStore(),
                authz=InMemoryAuthz(),
                audit=audit,
                subject="user:employee",
                agent_identity="ontos-cli",
            )
    finally:
        await audit.close()

    reopened = PostgresAuditEmitter.from_url(url)
    try:
        assert await reopened.verify_chain_async()

        from sqlalchemy import func, select

        async with reopened._engine.connect() as conn:
            result = await conn.execute(select(func.count()).select_from(reopened._table))
            assert result.scalar_one() == 1
    finally:
        await reopened.close()


async def test_candidate_inspection_audit_write_failure(
    tmp_path,
    monkeypatch,
) -> None:
    """No candidate result may be returned when persistent auditing fails."""
    from ontos.extraction import (
        CandidateProvenance,
        CandidateTriple,
        RejectedCandidate,
    )

    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-only-key")
    url = f"sqlite+aiosqlite:///{tmp_path / 'failed-audit.db'}"

    audit = PostgresAuditEmitter.from_url(url)
    await audit.initialize()

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", "view", "acl:finance")

    candidate = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="secret-person",
            subject_type="Person",
            subject_canonical_name="Secret Person",
            predicate="secret_relation",
            object_id="secret-company",
            object_type="Company",
            object_canonical_name="Secret Company",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="secret-document",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.9,
        ),
        acl_ref="acl:finance",
    )

    store = NetworkxStore()
    await store.add_candidates([candidate])

    async def failing_emit(**kwargs):
        raise RuntimeError("Audit database unavailable")

    monkeypatch.setattr(audit, "emit_async", failing_emit)

    try:
        with pytest.raises(RuntimeError, match="Audit database unavailable"):
            await inspect_candidates(
                candidate_store=store,
                authz=authz,
                audit=audit,
                subject="user:reviewer",
                agent_identity="ontos-cli-local-operator",
            )
    finally:
        await audit.close()
