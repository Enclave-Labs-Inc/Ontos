"""MultiConnector — composes several Connectors behind one Connector-shaped seam.

Mirrors the ``CascadeResolver`` composition pattern: Protocol-first,
no reflection, no plugin registry. The pipeline keeps its single-
connector contract; ``MultiConnector`` is the one place that fans
out across sub-connectors.

Primary use: a directory that mixes file types (``.txt`` / ``.md``
handled by ``TextConnector``, ``.pdf`` handled by a PDF connector)
can be ingested in one pass by wrapping both sub-connectors in a
``MultiConnector``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ontos.ingest.base import Connector, SourceDocument


class MultiConnector:
    """Yields documents from each sub-connector in order."""

    id: str = "ontos.ingest.multi-connector"
    source_kind: str = "multi"

    def __init__(self, connectors: list[Connector]) -> None:
        if not connectors:
            raise ValueError("MultiConnector requires at least one sub-connector")
        self._connectors = list(connectors)

    @property
    def sub_connectors(self) -> list[Connector]:
        return list(self._connectors)

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        for connector in self._connectors:
            async for doc in connector.iter_documents():
                yield doc
