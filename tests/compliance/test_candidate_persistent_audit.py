"""Verify candidate inspection records persist across audit connections."""

from __future__ import annotations

import pytest

from ontos.audit.postgres import PostgresAuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, InMemoryAuthz
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDenied,
    inspect_candidates,
)
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore


async def test_successful_inspection_persists_article12_record(
    tmp_path, monkeypatch
) -> None:
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
    finally:
        await reopened.close()


async def test_denied_inspection_persists_article12_record(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-only-key")
    url = f"sqlite+aiosqlite:///{tmp_path / 'candidate-denied.db'}"

    audit = PostgresAuditEmitter.from_url(url)
    await audit.initialize()

    try:
        with pytest.raises(CandidateInspectionDenied):
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
            result = await conn.execute(
                select(func.count()).select_from(reopened._table)
            )
            assert result.scalar_one() == 1
    finally:
        await reopened.close()
