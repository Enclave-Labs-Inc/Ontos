"""extraction subsystem for ontos.

Public API — nothing outside `ontos.extraction` may import extractor-
specific types (LlamaIndex, LangChain, or Scribe). Callers depend only
on the names re-exported here.
"""

from ontos.extraction.base import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
    ExtractionError,
    ExtractionInput,
    ExtractionResult,
    Extractor,
)
from ontos.extraction.llm_extractor import (
    LLMBackend,
    LlmExtractor,
    RawTriple,
)

__all__ = [
    "CandidateProvenance",
    "CandidateTriple",
    "RejectedCandidate",
    "Extractor",
    "ExtractionError",
    "ExtractionInput",
    "ExtractionResult",
    "LLMBackend",
    "LlmExtractor",
    "RawTriple",
]
