"""Connector Protocol — the source-system boundary.

A `Connector` yields `SourceDocument`s. Every document has:
- a stable `source_id` (persists across ingest runs so re-ingesting the
  same source updates rather than duplicates)
- `text` (the extractor payload)
- `metadata` (free-form; e.g. filename, author, mime type, filing date)
- `acl_ref` (optional; propagates directly to `Fact.acl_ref` for
  permission-aware traversal)

Connectors NEVER hit the ontos store directly. The ingest pipeline
consumes the connector's stream, hands each `SourceDocument` to the
extractor, resolves the resulting entities via the cascade, then
writes to the store. Keeping connectors state-free means they can be
added, swapped, or vendored per-tenant without touching the pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class SourceDocument(BaseModel):
    """One unit of work handed downstream by a Connector."""

    source_id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)
    acl_ref: str | None = None

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class Connector(Protocol):
    """Every connector implements this."""

    @property
    def id(self) -> str: ...

    @property
    def source_kind(self) -> str:
        """A stable label for the source system, e.g. `slack`, `drive`, `text-fixture`."""
        ...

    def iter_documents(self) -> AsyncIterator[SourceDocument]: ...


class ConnectorError(RuntimeError):
    """Raised when a connector can't reach or authenticate to its source."""
