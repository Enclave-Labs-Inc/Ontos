"""ingest subsystem for ontos.

Connectors yield SourceDocuments; the ingest pipeline downstream
(extractor → resolver → store) is unchanged regardless of which
connector produced the document.
"""

from ontos.ingest.base import Connector, ConnectorError, SourceDocument
from ontos.ingest.text import TextConnector

__all__ = [
    "Connector",
    "ConnectorError",
    "SourceDocument",
    "TextConnector",
]
