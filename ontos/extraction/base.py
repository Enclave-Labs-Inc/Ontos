"""Extractor Protocol — the Scribe-drop-in boundary.

Nothing downstream of `ontos.extraction` may import LlamaIndex, LangChain,
or any other extractor-specific type. Callers see only the types defined
here, so replacing the LlamaIndex-based extractor (M1.d) with
enclave-scribe (post-benchmark) is a config change, not a rewrite.

Every implementation MUST uphold these invariants (checked by
`tests/compliance/`):

- Every `Fact` in the returned `ExtractionResult` carries a fully
  populated `Provenance`. Never emit a partial one; if the extractor
  can't attribute a fact, drop the fact and add a warning.
- Every `Fact.subject_id` and `Fact.object_id` matches an `Entity.id`
  in the same result. No dangling references.
- `t_valid` defaults to `ingested_at` unless the source provides a
  stronger claim (e.g. a filing date, a message timestamp).
- Confidence labels (`EXTRACTED / INFERRED / AMBIGUOUS`) are calibrated
  per implementation — document the calibration in the impl module.
- On extraction failure the impl raises `ExtractionError` with the
  offending source, not a silently-empty result.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ontos.runtime.models import Entity, Fact


class ExtractionInput(BaseModel):
    """A single unit of work handed to an Extractor.

    `source_id` is the stable id of the source (doc id, message id, row
    id) — it becomes the `Provenance.source_id` on every fact this call
    produces. `metadata` is a free-form bag the extractor may consult
    (filename, author, mime type, filing date) — it is NOT persisted
    onto facts unless the extractor promotes specific fields.
    """

    source_id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class ExtractionResult(BaseModel):
    """What an Extractor returns for one `ExtractionInput`.

    `entities` are candidate entities the extractor identified in this
    source; the resolver (M1.d) may merge candidates across sources into
    canonical entities in the store. `facts` are triples between those
    entities; every `subject_id`/`object_id` must appear as some
    `Entity.id` in the same result. `warnings` are non-fatal issues
    surfaced for the audit trail — the impl chose to skip something it
    couldn't attribute, or saw a schema violation the pruner will drop.
    """

    entities: list[Entity] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class Extractor(Protocol):
    """The Scribe-drop-in interface.

    All implementations are async because extraction is I/O-bound (LLM
    calls today; Scribe endpoints later). `id` and `version` identify
    which extractor produced a fact so its provenance carries the
    right lineage and so audit can reconstruct the pipeline that ran
    at any point in time.
    """

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def extract(self, input: ExtractionInput) -> ExtractionResult: ...


class ExtractionError(RuntimeError):
    """Raised when extraction fails on a specific source.

    Loud failure by design — the Round-4 research found that LangChain's
    LLMGraphTransformer silently returns empty nodes/relationships on
    JSON-parse errors. That silent-empty is unacceptable for a
    compliance-facing pipeline: an empty extraction looks identical to
    "this source had nothing to extract" in the audit trail, so we
    would lose the ability to prove we tried.

    Wrappers of third-party extractors MUST convert their silent
    empties into an `ExtractionError` carrying `source_id`, a truncated
    sample of `text`, and the underlying error message.
    """

    def __init__(self, source_id: str, message: str, sample: str = "") -> None:
        self.source_id = source_id
        self.sample = sample[:500]
        super().__init__(f"extraction failed for source_id={source_id!r}: {message}")
