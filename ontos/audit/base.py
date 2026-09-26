"""AuditSink — the Article-12 audit contract, independent of storage.

Both `AuditEmitter` (in-memory) and `PostgresAuditEmitter` (persistent)
satisfy this Protocol. `build_server()` accepts either, so switching
between them is a config change.
"""

from __future__ import annotations

from typing import Protocol
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

    def verify_chain(self) -> bool: ...

    def __len__(self) -> int: ...
