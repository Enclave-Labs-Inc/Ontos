"""Integration tests: PostgresAuditEmitter against a real Postgres 16 via Testcontainers.

Skipped by default (marker `integration`); CI runs it explicitly and
skips cleanly if Docker isn't reachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from ontos.audit.postgres import PostgresAuditEmitter
from ontos.runtime.models import ArticleTwelveField

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_container():
    docker = pytest.importorskip("docker")
    testcontainers_postgres = pytest.importorskip("testcontainers.postgres")
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker not reachable: {exc}")

    container = testcontainers_postgres.PostgresContainer(image="postgres:16", driver="asyncpg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def emitter(postgres_container) -> AsyncIterator[PostgresAuditEmitter]:
    url = postgres_container.get_connection_url()
    emitter = PostgresAuditEmitter(
        engine=_engine_from_url(url),
        signing_key=b"postgres-audit-test-key",
    )
    await emitter.initialize()
    async with emitter._engine.begin() as conn:
        from ontos.audit.postgres import _audit_table

        await conn.execute(_audit_table.delete())
    try:
        yield emitter
    finally:
        await emitter.close()


def _engine_from_url(url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(url, future=True)


async def _emit(emitter: PostgresAuditEmitter, *, query_text: str) -> None:
    await emitter.emit_async(
        query_id=None,
        agent_identity="agent:test",
        acting_on_behalf_of="user:alice",
        query_text=query_text,
        tool_invoked="search",
        tool_arguments={"k": 5},
        result_fact_ids=["f1", "f2"],
        latency_ms=1.0,
        model_versions={"runtime": "0.0.1"},
        policy_decisions=[{"policy": "inmemory-authz", "result": "allow"}],
    )


async def test_emit_persists_all_article_twelve_fields(
    emitter: PostgresAuditEmitter,
) -> None:
    record = await emitter.emit_async(
        query_id=None,
        agent_identity="agent:a",
        acting_on_behalf_of="user:b",
        query_text="find X",
        tool_invoked="search",
        tool_arguments={"k": 10},
        result_fact_ids=["f1"],
        latency_ms=42.5,
        model_versions={"runtime": "0.0.1"},
        policy_decisions=[],
    )
    for field in ArticleTwelveField:
        assert field in record.article12

    got = await emitter.by_query_id_async(record.query_id)
    assert got is not None
    assert got.hash == record.hash
    for field in ArticleTwelveField:
        assert got.article12[field] == record.article12[field]


async def test_chain_is_hash_linked_in_postgres(emitter: PostgresAuditEmitter) -> None:
    r1 = await emitter.emit_async(
        query_id=None,
        agent_identity="a",
        acting_on_behalf_of=None,
        query_text="q1",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=[],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    r2 = await emitter.emit_async(
        query_id=None,
        agent_identity="a",
        acting_on_behalf_of=None,
        query_text="q2",
        tool_invoked="search",
        tool_arguments={},
        result_fact_ids=[],
        latency_ms=1.0,
        model_versions={},
        policy_decisions=[],
    )
    assert r1.prev_hash is None
    assert r2.prev_hash == r1.hash


async def test_verify_chain_passes_for_untampered(emitter: PostgresAuditEmitter) -> None:
    for i in range(5):
        await _emit(emitter, query_text=f"q{i}")
    assert await emitter.verify_chain_async()
    assert await emitter.count_async() == 5


async def test_verify_chain_fails_when_row_is_tampered(
    emitter: PostgresAuditEmitter,
) -> None:
    """Bypass the emitter and mutate a row directly; verify catches it."""
    await _emit(emitter, query_text="original")
    from ontos.audit.postgres import _audit_table

    async with emitter._engine.begin() as conn:
        result = await conn.execute(
            _audit_table.select().order_by(_audit_table.c.ordinal.asc()).limit(1)
        )
        row = result.mappings().first()
        assert row is not None
        await conn.execute(
            _audit_table.update()
            .where(_audit_table.c.query_id == row["query_id"])
            .values(hash="0" * 64)
        )
    assert not await emitter.verify_chain_async()


async def test_prune_expired_removes_old_records(
    emitter: PostgresAuditEmitter,
) -> None:
    for i in range(3):
        await _emit(emitter, query_text=f"q{i}")
    # Prune anything older than "now + 1 second" — everything qualifies.
    n_deleted = await emitter.prune_expired(datetime.now(UTC) + timedelta(seconds=1))
    assert n_deleted == 3
    assert await emitter.count_async() == 0


async def test_prune_leaves_recent_records_verifiable(
    emitter: PostgresAuditEmitter,
) -> None:
    # Retention window that keeps everything (guardrail against accidental full-wipe).
    for i in range(3):
        await _emit(emitter, query_text=f"q{i}")
    n_deleted = await emitter.prune_expired(datetime.now(UTC) - timedelta(days=1))
    assert n_deleted == 0
    assert await emitter.verify_chain_async()
    assert await emitter.count_async() == 3


async def test_sync_helpers_raise_helpful_error(emitter: PostgresAuditEmitter) -> None:
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="async-only"):
        emitter.verify_chain()
    with _pytest.raises(RuntimeError, match="async-only"):
        emitter.by_query_id(_uuid4())
    with _pytest.raises(RuntimeError, match="async-only"):
        len(emitter)


def _uuid4():
    from uuid import uuid4

    return uuid4()


async def test_emit_sync_signature_raises_pointer_to_async(
    emitter: PostgresAuditEmitter,
) -> None:
    with pytest.raises(RuntimeError, match="async-only"):
        emitter.emit(
            query_id=None,
            agent_identity="a",
            acting_on_behalf_of=None,
            query_text="x",
            tool_invoked="search",
            tool_arguments={},
            result_fact_ids=[],
            latency_ms=1.0,
            model_versions={},
            policy_decisions=[],
        )


async def test_concurrent_emits_do_not_fork_the_chain(
    emitter: PostgresAuditEmitter,
) -> None:
    """Without the advisory lock, concurrent emitters read the same prev_hash."""
    import asyncio

    await asyncio.gather(*(_emit(emitter, query_text=f"q{i}") for i in range(50)))

    assert await emitter.count_async() == 50
    assert await emitter.verify_chain_async()

    async with emitter._engine.connect() as conn:
        from sqlalchemy import func, select

        from ontos.audit.postgres import _audit_table

        result = await conn.execute(
            select(func.count()).select_from(_audit_table).where(_audit_table.c.prev_hash.is_(None))
        )
        assert result.scalar_one() == 1  # exactly one genesis record


async def test_schemas_hold_independent_chains(postgres_container) -> None:
    from sqlalchemy import text

    url = postgres_container.get_connection_url()
    engine = _engine_from_url(url)
    async with engine.begin() as conn:
        for schema in ("tenant_a", "tenant_b"):
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {schema}"))

    a = PostgresAuditEmitter(engine, b"key-a", schema="tenant_a")
    b = PostgresAuditEmitter(engine, b"key-b", schema="tenant_b")
    try:
        await a.initialize()
        await b.initialize()

        for i in range(3):
            await _emit(a, query_text=f"a{i}")
        record_b = await b.emit_async(
            query_id=None,
            agent_identity="agent:b",
            acting_on_behalf_of=None,
            query_text="b0",
            tool_invoked="search",
            tool_arguments={},
            result_fact_ids=[],
            latency_ms=1.0,
            model_versions={},
            policy_decisions=[],
        )

        assert await a.count_async() == 3
        assert await b.count_async() == 1
        assert record_b.prev_hash is None  # b's chain starts fresh
        assert await a.by_query_id_async(record_b.query_id) is None
        assert await a.verify_chain_async()
        assert await b.verify_chain_async()
    finally:
        await engine.dispose()
