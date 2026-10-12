"""Unit tests: PostgresAuditEmitter against the local sqlite+aiosqlite backend.

Exercises the real async SQLAlchemy engine (no mocks) so a missing async
runtime dependency — e.g. greenlet on Apple Silicon (#58) — fails here
instead of only on a contributor's machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.audit.postgres import PostgresAuditEmitter

_SIGNING_KEY_ENV = "ONTOS_TEST_AUDIT_SIGNING_KEY"


async def _emit(emitter: PostgresAuditEmitter, fact_ids: list[str]) -> None:
    await emitter.emit_async(
        query_id=None,
        agent_identity="ontos-cli",
        acting_on_behalf_of="user:alice",
        query_text="ontos export graphml",
        tool_invoked="ontos.export.graphml",
        tool_arguments={"include_acl_mask": False, "fact_count": len(fact_ids)},
        result_fact_ids=fact_ids,
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )


async def test_sqlite_emitter_persists_a_verifiable_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SIGNING_KEY_ENV, "sqlite-audit-test-key")
    db_path = tmp_path / "nested" / "audit.sqlite"

    emitter = PostgresAuditEmitter.from_url(
        f"sqlite+aiosqlite:///{db_path}",
        signing_key_env=_SIGNING_KEY_ENV,
    )
    try:
        await emitter.initialize()
        await _emit(emitter, ["fact-1"])
        await _emit(emitter, ["fact-2", "fact-3"])

        assert await emitter.count_async() == 2
        assert await emitter.verify_chain_async() is True
    finally:
        await emitter.close()

    assert db_path.exists()
