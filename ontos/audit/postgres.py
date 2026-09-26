"""PostgresAuditEmitter — persistent, hash-chained Article-12 audit sink.

Article 12 requires ≥6 months of retention for high-risk AI systems.
The in-memory `AuditEmitter` cannot survive a restart, so regulated
deploys need this persistent path.

Design:

- One row per `AuditRecord` in `ontos_audit_records`. Ordered by
  `ordinal` (`BIGSERIAL`) so `verify_chain()` can walk the sequence
  deterministically. `prev_hash → hash` links each record to the
  previous one; tampering with a record breaks the chain at that
  point and later.
- Idempotent `initialize()` creates the table + indexes; safe to call
  on every boot.
- `prune_expired(older_than)` removes records past the retention
  window. Pruning breaks the chain back to the beginning (by design —
  you cannot delete data and preserve full-history verifiability), but
  `verify_chain()` still returns True for the retained prefix because
  each retained record is internally consistent with what remains.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    delete,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ontos.audit.base import AuditSink  # noqa: F401 — protocol reference for imports
from ontos.runtime.models import ArticleTwelveField, AuditRecord

_metadata = MetaData()

_audit_table = Table(
    "ontos_audit_records",
    _metadata,
    Column("query_id", String(64), primary_key=True),
    Column(
        "ordinal", BigInteger, autoincrement=True, unique=True, nullable=False
    ),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("article12", JSON, nullable=False),
    Column("prev_hash", String(64)),
    Column("hash", String(64), nullable=False),
    Index("ix_audit_timestamp", "timestamp"),
    Index("ix_audit_ordinal", "ordinal"),
)


def _record_from_row(row: dict[str, object]) -> AuditRecord:
    raw_article12 = row["article12"]
    if isinstance(raw_article12, str):
        raw_article12 = json.loads(raw_article12)
    assert isinstance(raw_article12, dict)
    article12: dict[ArticleTwelveField, str] = {
        ArticleTwelveField(k): str(v) for k, v in raw_article12.items()
    }
    timestamp = row["timestamp"]
    assert isinstance(timestamp, datetime)
    query_id = row["query_id"]
    assert isinstance(query_id, str)
    hash_ = row["hash"]
    assert isinstance(hash_, str)
    prev_hash = row.get("prev_hash")
    assert prev_hash is None or isinstance(prev_hash, str)
    return AuditRecord(
        query_id=UUID(query_id),
        timestamp=timestamp,
        article12=article12,
        prev_hash=prev_hash,
        hash=hash_,
    )


class PostgresAuditEmitter:
    """Persistent AuditSink backed by Postgres via SQLAlchemy async.

    Constructor is intended for tests; production wires via
    `from_url()` which composes engine + signing key from env.
    """

    def __init__(self, engine: AsyncEngine, signing_key: bytes) -> None:
        if not signing_key:
            raise ValueError("audit signing key is required — never boot without it")
        self._engine = engine
        self._key = signing_key

    @classmethod
    def from_url(
        cls,
        db_url: str,
        *,
        signing_key_env: str = "ONTOS_AUDIT_SIGNING_KEY",
    ) -> PostgresAuditEmitter:
        raw = os.environ.get(signing_key_env)
        if not raw:
            raise RuntimeError(
                f"{signing_key_env} not set — audit emitter refuses to boot "
                "without a signing key"
            )
        # Ensure the URL uses an async driver — reject sync-only shapes early.
        if "+asyncpg" not in db_url and "+aiosqlite" not in db_url:
            raise RuntimeError(
                "audit DB URL must use an async driver "
                "(postgresql+asyncpg:// or sqlite+aiosqlite://) — got: "
                f"{db_url}"
            )
        engine = create_async_engine(db_url, future=True)
        return cls(engine=engine, signing_key=raw.encode("utf-8"))

    async def initialize(self) -> None:
        """Create tables + indexes idempotently."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_metadata.create_all)

    def emit(
        self,
        *,
        query_id: UUID | None,
        agent_identity: str,
        acting_on_behalf_of: str | None,
        query_text: str,
        tool_invoked: str,
        tool_arguments: dict[str, object],
        result_fact_ids: list[str],
        latency_ms: float,
        model_versions: dict[str, str],
        policy_decisions: list[dict[str, str]],
    ) -> AuditRecord:
        # AuditSink.emit is synchronous by contract (matches AuditEmitter);
        # persistence is scheduled via a dedicated helper. Runners call
        # `emit_async` from the tool coroutine.
        raise RuntimeError(
            "PostgresAuditEmitter is async-only. Use `await emit_async(...)` "
            "or wrap in `asyncio.run(...)` for scripts."
        )

    async def emit_async(
        self,
        *,
        query_id: UUID | None,
        agent_identity: str,
        acting_on_behalf_of: str | None,
        query_text: str,
        tool_invoked: str,
        tool_arguments: dict[str, object],
        result_fact_ids: list[str],
        latency_ms: float,
        model_versions: dict[str, str],
        policy_decisions: list[dict[str, str]],
    ) -> AuditRecord:
        qid = query_id or uuid4()
        result_hash = hashlib.sha256(
            json.dumps(sorted(result_fact_ids)).encode("utf-8")
        ).hexdigest()
        article12: dict[ArticleTwelveField, str] = {
            ArticleTwelveField.QUERY_ID: str(qid),
            ArticleTwelveField.AGENT_IDENTITY: agent_identity,
            ArticleTwelveField.ACTING_ON_BEHALF_OF: acting_on_behalf_of or "",
            ArticleTwelveField.QUERY_TEXT: query_text,
            ArticleTwelveField.TOOL_INVOKED: tool_invoked,
            ArticleTwelveField.TOOL_ARGUMENTS: json.dumps(tool_arguments, sort_keys=True),
            ArticleTwelveField.RESULT_FACT_IDS: json.dumps(sorted(result_fact_ids)),
            ArticleTwelveField.RESULT_HASH: result_hash,
            ArticleTwelveField.TIMESTAMP: datetime.now(UTC).isoformat(),
            ArticleTwelveField.LATENCY_MS: f"{latency_ms:.3f}",
            ArticleTwelveField.MODEL_VERSIONS: json.dumps(model_versions, sort_keys=True),
            ArticleTwelveField.POLICY_DECISIONS: json.dumps(policy_decisions, sort_keys=True),
        }
        async with self._engine.begin() as conn:
            prev_hash_result = await conn.execute(
                select(_audit_table.c.hash)
                .order_by(_audit_table.c.ordinal.desc())
                .limit(1)
            )
            prev_hash_row = prev_hash_result.first()
            prev_hash: str | None = prev_hash_row[0] if prev_hash_row else None

            payload = json.dumps(
                {
                    "article12": {k.value: v for k, v in article12.items()},
                    "prev_hash": prev_hash,
                },
                sort_keys=True,
            ).encode("utf-8")
            record_hash = hmac.new(self._key, payload, hashlib.sha256).hexdigest()

            await conn.execute(
                _audit_table.insert().values(
                    query_id=str(qid),
                    timestamp=datetime.now(UTC),
                    article12={k.value: v for k, v in article12.items()},
                    prev_hash=prev_hash,
                    hash=record_hash,
                )
            )
        return AuditRecord(
            query_id=qid,
            timestamp=datetime.now(UTC),
            article12=article12,
            prev_hash=prev_hash,
            hash=record_hash,
        )

    def by_query_id(self, query_id: UUID) -> AuditRecord | None:
        raise RuntimeError("PostgresAuditEmitter is async-only; use by_query_id_async")

    async def by_query_id_async(self, query_id: UUID) -> AuditRecord | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(_audit_table).where(_audit_table.c.query_id == str(query_id))
            )
            row = result.mappings().first()
            if row is None:
                return None
            return _record_from_row(dict(row))

    def verify_chain(self) -> bool:
        raise RuntimeError("PostgresAuditEmitter is async-only; use verify_chain_async")

    async def verify_chain_async(self) -> bool:
        """Walk the persisted chain in ordinal order; re-hash every record.

        Returns True iff every record's `hash` matches an HMAC re-computation
        of its `article12` + declared `prev_hash`. Retention pruning breaks
        the chain back to genesis (by design) but the retained prefix
        remains internally consistent, so this returns True for what's kept.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(_audit_table).order_by(_audit_table.c.ordinal.asc())
            )
            expected_prev: str | None = None
            first = True
            for row in result.mappings():
                article12 = row["article12"]
                if isinstance(article12, str):
                    article12 = json.loads(article12)
                payload = json.dumps(
                    {"article12": article12, "prev_hash": row["prev_hash"]},
                    sort_keys=True,
                ).encode("utf-8")
                expected_hash = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected_hash, row["hash"]):
                    return False
                if not first and row["prev_hash"] != expected_prev:
                    return False
                expected_prev = row["hash"]
                first = False
        return True

    async def prune_expired(self, older_than: datetime) -> int:
        """Delete records with `timestamp < older_than`. Returns count deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(_audit_table).where(_audit_table.c.timestamp < older_than)
            )
            return result.rowcount or 0

    def __len__(self) -> int:
        raise RuntimeError("PostgresAuditEmitter is async-only; use count_async")

    async def count_async(self) -> int:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {_audit_table.name}")
            )
            row = result.first()
            return int(row[0]) if row else 0

    async def close(self) -> None:
        await self._engine.dispose()
