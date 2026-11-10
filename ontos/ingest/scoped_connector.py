"""In-memory connector for documents selected by scoped preflight."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from ontos.ingest.base import SourceDocument
from ontos.storage.sources import SourceRecord


class PreparedScopedConnector:
    """Yield only prepared documents; never access the filesystem."""

    id = "ontos.ingest.scoped-prepared"
    source_kind = "scoped-local"

    def __init__(self, documents: Sequence[SourceDocument]) -> None:
        identities = [SourceRecord(doc.source_id, doc.acl_ref) for doc in documents]

        if not identities or len(set(identities)) != len(identities):
            raise ValueError("Invalid prepared source selection")

        self._documents = tuple(documents)

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        for document in self._documents:
            yield document
