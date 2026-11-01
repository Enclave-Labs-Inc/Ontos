"""AuditSink — the Article-12 audit contract, independent of storage.

Both `AuditEmitter` (in-memory) and `PostgresAuditEmitter` (persistent)
satisfy this Protocol. `build_server()` accepts either, so switching
between them is a config change.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Protocol
from uuid import UUID

from ontos.runtime.models import AuditRecord


class AuditSink(Protocol):
    """Structural interface every audit emitter satisfies."""

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
    ) -> AuditRecord: ...

    def by_query_id(self, query_id: UUID) -> AuditRecord | None: ...

    def derive_key(self, label: bytes) -> bytes:
        """Return a purpose-specific key derived from the signing key."""
        ...

    def verify_chain(self) -> bool: ...

    def __len__(self) -> int: ...


def derive_subkey(signing_key: bytes, label: bytes) -> bytes:
    """Derive an independent key for `label` from the audit signing key.

    Keeps the signing key single-purpose: values keyed with a subkey
    (e.g. audit references to sensitive records) can't be used to forge
    chain hashes, and vice versa.
    """
    return hmac.new(signing_key, b"ontos.audit.subkey.v1:" + label, hashlib.sha256).digest()


class AuditUnavailableError(RuntimeError):
    """The audit sink could not record or read an Article-12 record."""


async def emit_record(audit: AuditSink, **fields: Any) -> AuditRecord:
    """Emit through `audit`, awaiting persistent (async-only) sinks.

    `PostgresAuditEmitter.emit` raises by design, so request paths call
    this instead of `audit.emit` to work with either sink.
    """
    emit_async = getattr(audit, "emit_async", None)
    if emit_async is not None:
        record: AuditRecord = await emit_async(**fields)
        return record
    return audit.emit(**fields)


async def lookup_record(audit: AuditSink, query_id: UUID) -> AuditRecord | None:
    """`by_query_id` for either sink; see `emit_record`."""
    by_query_id_async = getattr(audit, "by_query_id_async", None)
    if by_query_id_async is not None:
        record: AuditRecord | None = await by_query_id_async(query_id)
        return record
    return audit.by_query_id(query_id)
