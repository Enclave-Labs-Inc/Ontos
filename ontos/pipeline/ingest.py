"""IngestPipeline — the wire that connects M4's parts.

Connector → LlmExtractor (M1.d) → CascadeResolver (M4.a) → GraphStore
(M1.b). This is the piece that turns Ontos from "four islands you have
to stitch together" into "point it at a corpus and let it ingest."

Per-doc error policy is configurable:

- `FAIL_FAST` (default) — the first `ExtractionError` propagates. Right
  for interactive ingest where operators want to see the first broken
  source and fix it.
- `SKIP_AND_LOG` — errored sources land in the `IngestReport.errors`
  list; ingest continues. Right for batch ingest of large corpora
  where isolated bad docs shouldn't kill the run.

Never silent-drops a source. Every skipped source shows up in the
report so the operator sees exactly what didn't land.
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ontos.extraction import ExtractionError, ExtractionInput, Extractor
from ontos.ingest import Connector
from ontos.ontology import Ontology
from ontos.resolver import Resolver
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore
from ontos.supersession import CardinalitySupersessionPolicy, SupersessionPolicy

log = structlog.get_logger()


class ErrorPolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    SKIP_AND_LOG = "skip_and_log"


class IngestError(BaseModel):
    source_id: str
    reason: str

    model_config = ConfigDict(frozen=True)


class IngestReport(BaseModel):
    """What the pipeline returns after a run."""

    documents_seen: int = 0
    documents_extracted: int = 0
    facts_written: int = 0
    entities_merged: int = 0
    facts_superseded: int = 0
    facts_reaffirmed: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[IngestError] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class IngestPipeline:
    """Composes Connector + Extractor + Resolver + GraphStore into a single run.

    The pipeline is stateless — you can call `run()` multiple times
    with the same instance or discard it after one call. All state
    lives in the components it composes.

    `supersession` closes older active facts when the ontology says a
    newer one contradicts them (bitemporal correctness; GitHub #21).
    When omitted it defaults to `CardinalitySupersessionPolicy(ontology)`
    — a compliance-first default. Operators who want to stack every
    ingest can pass `NullSupersessionPolicy()`.
    """

    def __init__(
        self,
        *,
        connector: Connector,
        extractor: Extractor,
        resolver: Resolver | None,
        store: GraphStore,
        ontology: Ontology,
        supersession: SupersessionPolicy | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.FAIL_FAST,
    ) -> None:
        self._connector = connector
        self._extractor = extractor
        self._resolver = resolver
        self._store = store
        self._ontology = ontology
        self._supersession: SupersessionPolicy = (
            supersession if supersession is not None else CardinalitySupersessionPolicy(ontology)
        )
        self._error_policy = error_policy

    async def run(self) -> IngestReport:
        documents_seen = 0
        documents_extracted = 0
        facts_written = 0
        facts_superseded = 0
        facts_reaffirmed = 0
        entities_merged = 0
        warnings: list[str] = []
        errors: list[IngestError] = []

        async for doc in self._connector.iter_documents():
            documents_seen += 1
            try:
                extraction = await self._extractor.extract(
                    ExtractionInput(
                        source_id=doc.source_id,
                        text=doc.text,
                        metadata=doc.metadata,
                    )
                )
            except ExtractionError as exc:
                if self._error_policy is ErrorPolicy.FAIL_FAST:
                    raise
                errors.append(IngestError(source_id=doc.source_id, reason=str(exc)))
                continue

            documents_extracted += 1
            warnings.extend(extraction.warnings)

            if self._resolver is not None and extraction.entities:
                resolution = await self._resolver.resolve(list(extraction.entities))
                entities_merged += sum(max(len(m.merged_ids) - 1, 0) for m in resolution.merges)

            for fact in extraction.facts:
                write_fact: Fact = fact
                if doc.acl_ref is not None and write_fact.acl_ref is None:
                    write_fact = write_fact.model_copy(update={"acl_ref": doc.acl_ref})

                decision = await self._supersession.decide(write_fact, self._store, self._ontology)

                if decision.skip_add:
                    facts_reaffirmed += 1
                    continue

                await self._store.add_fact(write_fact)
                facts_written += 1

                for old in decision.facts_to_close:
                    await self._store.close_fact(
                        old.id,
                        t_invalid=write_fact.ingested_at,
                        superseded_by=write_fact.id,
                    )
                    facts_superseded += 1

                if decision.record is not None:
                    log.warning(
                        "fact superseded",
                        new_fact_id=str(write_fact.id),
                        closed_fact_ids=[str(fid) for fid in decision.record.closed_fact_ids],
                        subject_id=write_fact.subject_id,
                        predicate=write_fact.predicate,
                        policy_id=decision.record.policy_id,
                    )

        return IngestReport(
            documents_seen=documents_seen,
            documents_extracted=documents_extracted,
            facts_written=facts_written,
            entities_merged=entities_merged,
            facts_superseded=facts_superseded,
            facts_reaffirmed=facts_reaffirmed,
            warnings=warnings,
            errors=errors,
        )
