"""ingest subsystem for ontos.

Connectors yield SourceDocuments; the ingest pipeline downstream
(extractor → resolver → store) is unchanged regardless of which
connector produced the document.
"""

from ontos.ingest.base import Connector, ConnectorError, SourceDocument
from ontos.ingest.llamacloud_pdf import LlamaParsePdfConnector
from ontos.ingest.multi import MultiConnector
from ontos.ingest.text import TextConnector

__all__ = [
    "Connector",
    "ConnectorError",
    "LlamaParsePdfConnector",
    "MultiConnector",
    "SourceDocument",
    "TextConnector",
]
