"""Trusted source registry for scoped re-ingestion.

Internal operator/write-path interface, not a user-facing graph API.
Callers must enforce operator authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    acl_ref: str | None


class SourceAclConflictError(RuntimeError):
    """An existing source cannot change ACL."""


@runtime_checkable
class SourceRegistry(Protocol):
    async def get_source_record(
        self, source_id: str, *, acl_ref: str | None
    ) -> SourceRecord | None:
        """Privileged source lookup."""
        ...

    async def register_source(self, record: SourceRecord) -> None:
        """Idempotent registration of an exact (source_id, acl_ref) pair."""
        ...
