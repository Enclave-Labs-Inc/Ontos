"""pipeline subsystem for ontos.

Composes the four M4 pieces (Connector, Extractor, Resolver, Store)
into a single ingest run.
"""

from ontos.pipeline.ingest import (
    ErrorPolicy,
    IngestError,
    IngestPipeline,
    IngestReport,
)

__all__ = [
    "ErrorPolicy",
    "IngestError",
    "IngestPipeline",
    "IngestReport",
]
