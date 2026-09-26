"""TextConnector — the dev/test connector. Yields a fixed dict of documents.

Every real-world Connector implementation (Slack, Drive, Confluence,
Jira, Salesforce, GitHub) ultimately reduces to "iterate documents
from a source"; TextConnector lets us exercise the ingest pipeline
end-to-end without hitting a real service. Real connectors follow the
same Protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ontos.ingest.base import SourceDocument


class TextConnector:
    """Yields `SourceDocument`s from an in-memory dict."""

    id: str = "ontos.ingest.text-connector"
    source_kind: str = "text-fixture"

    def __init__(
        self,
        documents: dict[str, str],
        *,
        acl_ref_by_source: dict[str, str] | None = None,
        metadata_by_source: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._documents = dict(documents)
        self._acl_refs = dict(acl_ref_by_source or {})
        self._metadata = dict(metadata_by_source or {})

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        for source_id, text in self._documents.items():
            yield SourceDocument(
                source_id=source_id,
                text=text,
                metadata=self._metadata.get(source_id, {}),
                acl_ref=self._acl_refs.get(source_id),
            )
