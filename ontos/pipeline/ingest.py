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

from ontos.extraction import ExtractionError, ExtractionInput, ExtractionResult, Extractor
from ontos.ingest import Connector
from ontos.ontology import Ontology
from ontos.resolver import Resolver
from ontos.runtime.models import Entity, Fact
from ontos.storage.base import CandidateStore, CrossAclUpsertError, GraphStore
from ontos.storage.models import StoredEntity
from ontos.supersession import (
    CardinalitySupersessionPolicy,
    SupersessionPolicy,
    SupersessionRecord,
)

log = structlog.get_logger()


class _SameSourceIdentityConflictError(RuntimeError):
    """Unsafe raw-ID reuse detected during same-source reconciliation."""


def _entity_identity_key(
    entity_type: str | None,
    canonical_name: str | None,
) -> tuple[str, str] | None:
    """Conservative identity key for same-source re-ingest reconciliation."""
    if not entity_type or not canonical_name:
        return None

    entity_type = entity_type.strip()
    canonical_name = canonical_name.strip()
    if not entity_type or not canonical_name:
        return None

    return entity_type.casefold(), canonical_name.casefold()


def _reconcile_same_source_entity_ids(
    extraction: ExtractionResult,
    previous: list[StoredEntity],
) -> ExtractionResult:
    """Reuse prior raw IDs for unambiguous entities from the same source.

    This does not perform cross-source entity resolution. A raw ID is reused
    only when exactly one previous entity and exactly one newly extracted
    entity share the same (type, canonical_name) key.
    """
    previous_by_key: dict[tuple[str, str], list[StoredEntity]] = {}
    for stored_entity in previous:
        key = _entity_identity_key(stored_entity.type, stored_entity.canonical_name)
        if key is not None:
            previous_by_key.setdefault(key, []).append(stored_entity)

    current_by_key: dict[tuple[str, str], list[Entity]] = {}
    for current_entity in extraction.entities:
        key = _entity_identity_key(current_entity.type, current_entity.canonical_name)
        if key is not None:
            current_by_key.setdefault(key, []).append(current_entity)

    reused_targets: set[str] = set()
    id_map: dict[str, str] = {}

    for key, current_entities in current_by_key.items():
        previous_entities = previous_by_key.get(key, [])

        # Ambiguous namesakes or duplicate candidates: do nothing.
        if len(current_entities) != 1 or len(previous_entities) != 1:
            continue

        current = current_entities[0]
        previous_entity = previous_entities[0]

        if current.id == previous_entity.id:
            continue

        if previous_entity.id in reused_targets:
            continue

        id_map[current.id] = previous_entity.id
        reused_targets.add(previous_entity.id)

    if not id_map:
        return extraction

    # Apply the mapping conceptually to the whole extraction before
    # changing anything. This deliberately allows permutations such as
    # current 1->previous 2 and current 2->previous 1, while refusing a
    # mapping that would collapse two current entities onto one raw id.
    final_ids = [id_map.get(entity.id, entity.id) for entity in extraction.entities]

    # Never fall back to the original extraction when reconciliation would
    # collapse two current entities onto one raw ID. Doing so could let an
    # LLM-reused ID overwrite the meaning of an already stored entity.
    if len(final_ids) != len(set(final_ids)):
        raise _SameSourceIdentityConflictError(
            "same-source entity identity conflict; document skipped"
        )

    # A raw ID that already exists for this source may only retain the same
    # semantic identity. This also catches the case where the model reuses an
    # old ID for a completely different entity without creating a duplicate
    # final ID.
    previous_by_id = {entity.id: entity for entity in previous}

    for current, final_id in zip(extraction.entities, final_ids, strict=True):
        prior = previous_by_id.get(final_id)
        if prior is None:
            continue

        current_key = _entity_identity_key(
            current.type,
            current.canonical_name,
        )
        prior_key = _entity_identity_key(
            prior.type,
            prior.canonical_name,
        )

        # Preserve migration compatibility for legacy nodes that have the
        # same raw ID but are missing typed/canonical entity attributes.
        if current.id == final_id and prior_key is None:
            continue

        if current_key != prior_key:
            raise _SameSourceIdentityConflictError(
                "same-source entity identity conflict; document skipped"
            )

    entities = [
        entity.model_copy(update={"id": id_map.get(entity.id, entity.id)})
        for entity in extraction.entities
    ]

    facts = [
        fact.model_copy(
            update={
                "subject_id": id_map.get(fact.subject_id, fact.subject_id),
                "object_id": id_map.get(fact.object_id, fact.object_id),
            }
        )
        for fact in extraction.facts
    ]

    return extraction.model_copy(
        update={
            "entities": entities,
            "facts": facts,
        }
    )


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
    candidates_retained: int = 0
    entities_merged: int = 0
    merges_recorded: int = 0
    facts_superseded: int = 0
    facts_reaffirmed: int = 0
    facts_historical: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[IngestError] = Field(default_factory=list)
    supersession_records: list[SupersessionRecord] = Field(default_factory=list)

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
        candidate_store: CandidateStore | None = None,
    ) -> None:
        self._connector = connector
        self._extractor = extractor
        self._resolver = resolver
        self._store = store
        self._ontology = ontology
        self._supersession: SupersessionPolicy = (
            supersession if supersession is not None else CardinalitySupersessionPolicy()
        )
        self._error_policy = error_policy
        self._candidate_store = candidate_store

    async def run(self) -> IngestReport:
        documents_seen = 0
        documents_extracted = 0
        facts_written = 0
        candidates_retained = 0
        facts_superseded = 0
        facts_reaffirmed = 0
        facts_historical = 0
        entities_merged = 0
        merges_recorded = 0
        warnings: list[str] = []
        errors: list[IngestError] = []
        supersession_records: list[SupersessionRecord] = []

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
                # Backend exceptions can contain document text, model
                # responses, or raw entity identifiers. Do not copy their
                # string representation into the public ingestion report.
                reason = (
                    "LLM returned zero triples for non-empty input"
                    if exc.kind == "empty"
                    else "extraction failed; document skipped"
                )
                errors.append(IngestError(source_id=doc.source_id, reason=reason))
                continue

            # Stabilize LLM-chosen raw IDs across re-ingests of the same
            # source before ACL checks, entity writes, resolution, or
            # supersession. Cross-source identity resolution remains the
            # resolver's responsibility.
            try:
                previous_entities = await self._store.entities_for_source(
                    doc.source_id,
                    acl_ref=doc.acl_ref,
                )
                extraction = _reconcile_same_source_entity_ids(
                    extraction,
                    previous_entities,
                )
            except _SameSourceIdentityConflictError as exc:
                if self._error_policy is ErrorPolicy.FAIL_FAST:
                    raise
                errors.append(
                    IngestError(
                        source_id=doc.source_id,
                        reason=str(exc),
                    )
                )
                log.warning(
                    "same-source-entity-identity-conflict",
                    source_id=doc.source_id,
                )
                continue

            documents_extracted += 1
            warnings.extend(extraction.warnings)

            # #38: upsert every extracted entity before writing facts so
            # every Fact.subject_id / Fact.object_id has a backing typed
            # node in the store. Pre-resolution semantics — facts
            # reference raw ids today, so writing raw candidates (not
            # canonicals) means no fact-rewriting is needed. The
            # resolver's MergeRecord still tracks "these are the same"
            # for the merge-count tally below and for future
            # consolidation work.
            #
            # Propagate the doc's ACL to the entity on first write so
            # entities extracted only from restricted docs don't leak
            # via a future entity-attribute read path (#32's exporter).
            #
            # #48 + PR #49 review: pre-check EVERY extracted entity's
            # ACL against the stored state BEFORE any write. Pre-review
            # the loop wrote entities one-by-one and raised partway
            # through — earlier entities would land with this doc's ACL
            # while the conflicting one aborted the rest, breaking the
            # "whole-doc skip" invariant. Pre-check makes "no writes
            # from a doc with any cross-ACL conflict" true.
            #
            # The pre-check is advisory; upsert_entity itself still
            # enforces the ACL contract to close any TOCTOU window
            # between pre-check and write (concurrent writers).
            conflict: tuple[str, str | None] | None = None  # (entity_id, stored_acl)
            for entity in extraction.entities:
                stored_acl = await self._store.entity_acl(entity.id)
                if stored_acl is not None and stored_acl != doc.acl_ref:
                    conflict = (entity.id, stored_acl)
                    break
            if conflict is not None:
                conflict_entity_id, conflict_stored_acl = conflict
                if self._error_policy is ErrorPolicy.FAIL_FAST:
                    raise CrossAclUpsertError(
                        entity_id=conflict_entity_id,
                        stored_acl=conflict_stored_acl,
                        attempted_acl=doc.acl_ref,
                    )
                # Generic reason — do NOT leak the stored ACL or entity
                # id through IngestReport.errors, which can be returned
                # to a caller who doesn't hold the stored ACL. The
                # detailed stored_acl + attempted_acl pair lives in the
                # audit log (`cross-acl-upsert-rejected` structlog
                # event), gated by log-sink access controls instead.
                errors.append(
                    IngestError(
                        source_id=doc.source_id,
                        reason="cross-ACL entity conflict; document skipped",
                    )
                )
                log.warning(
                    "cross-acl-upsert-rejected",
                    source_id=doc.source_id,
                    stage="pre-check",
                    stored_acl=conflict_stored_acl,
                    attempted_acl=doc.acl_ref,
                )
                continue

            # Pre-check passed: safe to write all entities. upsert_entity
            # may still raise under a concurrent writer race (TOCTOU) —
            # route the same way for defense in depth.
            try:
                for entity in extraction.entities:
                    await self._store.upsert_entity(entity, acl_ref=doc.acl_ref)
            except CrossAclUpsertError:
                if self._error_policy is ErrorPolicy.FAIL_FAST:
                    raise
                errors.append(
                    IngestError(
                        source_id=doc.source_id,
                        reason="cross-ACL entity conflict; document skipped",
                    )
                )
                continue

            # Retain rejected candidates only after the existing
            # cross-ACL checks and entity writes have succeeded.
            # Extractor-supplied ACL values are never trusted.
            if self._candidate_store is not None and extraction.rejected:
                stamped_candidates = [
                    candidate.model_copy(update={"acl_ref": doc.acl_ref})
                    for candidate in extraction.rejected
                ]
                # Storage failures deliberately propagate. Retention
                # must never silently lose rejected candidates.
                await self._candidate_store.add_candidates(stamped_candidates)
                candidates_retained += len(stamped_candidates)

            if self._resolver is not None and extraction.entities:
                resolution = await self._resolver.resolve(list(extraction.entities))
                entities_merged += sum(max(len(m.merged_ids) - 1, 0) for m in resolution.merges)
                # #46: persist every merge decision the resolver cascade
                # emits so #32's exporter + Article-12 audit tools can
                # walk the trail across process restarts. Idempotent at
                # the store layer on (canonical, merged, resolver_id)
                # keys — re-ingest produces one record, not N.
                for merge in resolution.merges:
                    await self._store.record_merge(merge)
                    merges_recorded += 1

            for fact in extraction.facts:
                write_fact: Fact = fact
                if doc.acl_ref is not None and write_fact.acl_ref is None:
                    write_fact = write_fact.model_copy(update={"acl_ref": doc.acl_ref})

                decision = await self._supersession.decide(write_fact, self._store, self._ontology)

                if decision.record is not None:
                    supersession_records.append(decision.record)

                if decision.skip_add:
                    facts_reaffirmed += 1
                    log.info(
                        "fact re-affirmed (idempotent re-ingest)",
                        new_fact_id=str(write_fact.id),
                        subject_id=write_fact.subject_id,
                        predicate=write_fact.predicate,
                        object_id=write_fact.object_id,
                        source_id=write_fact.provenance.source_id,
                        policy_id=self._supersession.id,
                        reason=decision.record.reason if decision.record else "",
                    )
                    continue

                # Historical-ingest override — write the new fact pre-closed.
                if decision.new_fact_t_invalid is not None:
                    write_fact = write_fact.model_copy(
                        update={
                            "t_invalid": decision.new_fact_t_invalid,
                            "superseded_by": decision.new_fact_superseded_by,
                        }
                    )
                    facts_historical += 1
                    log.warning(
                        "fact ingested as historical (backfill / out-of-order)",
                        new_fact_id=str(write_fact.id),
                        subject_id=write_fact.subject_id,
                        predicate=write_fact.predicate,
                        superseded_by=str(decision.new_fact_superseded_by),
                        t_invalid=str(decision.new_fact_t_invalid),
                        policy_id=self._supersession.id,
                        reason=decision.record.reason if decision.record else "",
                    )

                await self._store.add_fact(write_fact)
                facts_written += 1

                for old in decision.facts_to_close:
                    try:
                        await self._store.close_fact(
                            old.id,
                            t_invalid=write_fact.ingested_at,
                            superseded_by=write_fact.id,
                        )
                    except Exception as exc:
                        # Non-atomic add+close (deferred follow-up: a
                        # supersede() atomic store method). Make partial
                        # failure loud so operators can repair state.
                        log.error(
                            "close_fact failed after add_fact — "
                            "store may be in double-active state for this scope",
                            new_fact_id=str(write_fact.id),
                            old_fact_id=str(old.id),
                            subject_id=write_fact.subject_id,
                            predicate=write_fact.predicate,
                            error=str(exc),
                        )
                        raise
                    facts_superseded += 1

                if decision.facts_to_close:
                    log.warning(
                        "fact superseded",
                        new_fact_id=str(write_fact.id),
                        closed_fact_ids=[str(f.id) for f in decision.facts_to_close],
                        subject_id=write_fact.subject_id,
                        predicate=write_fact.predicate,
                        policy_id=self._supersession.id,
                        reason=decision.record.reason if decision.record else "",
                    )

        return IngestReport(
            documents_seen=documents_seen,
            documents_extracted=documents_extracted,
            facts_written=facts_written,
            candidates_retained=candidates_retained,
            entities_merged=entities_merged,
            merges_recorded=merges_recorded,
            facts_superseded=facts_superseded,
            facts_reaffirmed=facts_reaffirmed,
            facts_historical=facts_historical,
            warnings=warnings,
            errors=errors,
            supersession_records=supersession_records,
        )
