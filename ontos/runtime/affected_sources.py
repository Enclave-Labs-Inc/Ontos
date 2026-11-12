"""Discover the sources an approved ontology change affects.

Read-only planning step between `ontos ontology approve` and
`ontos reingest`. Evidence is limited to candidates the approver may
VIEW; nothing about other sources is returned, counted or audited.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from ontos.audit.base import AuditSink, emit_record
from ontos.authz import AuthzBackend
from ontos.migration.approval_audit import verify_signed_migration_record
from ontos.migration.records import (
    AddPatternChange,
    MapToExistingChange,
    MigrationRecord,
)
from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
from ontos.runtime.ontology_approval import (
    _collect_authorized_approval_candidates,
    _recheck_approval_permissions,
)
from ontos.runtime.scoped_reingest import _scope_reference
from ontos.storage.base import CandidateStore, StoredCandidate
from ontos.storage.sources import SourceRecord, SourceRegistry

AffectedSourcesDecision = Literal[
    "allow",
    "deny",
    "invalid_request",
    "authorization_error",
    "storage_error",
    "failure",
]


class AffectedSourcesDeniedError(PermissionError):
    """Affected-source discovery was not authorized."""


class AffectedSourcesRequestError(ValueError):
    """The migration record cannot be used for discovery."""


@dataclass(frozen=True)
class AffectedSources:
    """Sources ready for scoped re-ingest, and sources not yet registered."""

    registered: tuple[SourceRecord, ...]
    unregistered: tuple[SourceRecord, ...]


def evidence_pattern(record: MigrationRecord) -> tuple[str, str, str]:
    """The raw (subject_type, predicate, object_type) the approval was based on."""
    change = record.approved_changes[0]
    if isinstance(change, AddPatternChange):
        predicate = change.predicate
    elif isinstance(change, MapToExistingChange):
        predicate = change.observed_predicate
    else:
        predicate = change.relation_type
    return (change.subject_type, predicate, change.object_type)


def _matching(
    records: list[StoredCandidate],
    *,
    ontology_id: str,
    pattern: tuple[str, str, str],
) -> list[StoredCandidate]:
    matched: list[StoredCandidate] = []
    for stored in records:
        candidate = stored.candidate
        triple = candidate.raw_triple
        if candidate.reason != "pattern_not_in_ontology":
            continue
        # Any version of the same ontology: a source rejected under an
        # older version is still missing the now-approved relationship.
        if candidate.provenance.ontology_id != ontology_id:
            continue
        if (triple.subject_type, triple.predicate, triple.object_type) == pattern:
            matched.append(stored)
    return matched


def _sorted_sources(sources: set[SourceRecord]) -> tuple[SourceRecord, ...]:
    return tuple(sorted(sources, key=lambda item: (item.source_id, item.acl_ref or "")))


async def _audit_discovery(
    *,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    identity_assurance: str,
    decision: AffectedSourcesDecision,
    sources: tuple[SourceRecord, ...] = (),
    registered_count: int = 0,
    started: float,
) -> None:
    arguments: dict[str, object] = {"identity_assurance": identity_assurance}
    if decision == "allow":
        # Same keyed scope reference as `ontos reingest`, so discovery
        # and the re-ingest it plans can be correlated in the chain.
        arguments.update(
            affected_count=len(sources),
            registered_count=registered_count,
            scope_ref=_scope_reference(audit, sources) if sources else None,
        )
    await emit_record(
        audit,
        query_id=None,
        agent_identity=agent_identity,
        acting_on_behalf_of=subject or None,
        query_text="ontology affected-source discovery",
        tool_invoked="ontos.ontology.affected_sources",
        tool_arguments=arguments,
        result_fact_ids=[],
        latency_ms=(time.perf_counter() - started) * 1000,
        model_versions={"component": "affected-sources-v1"},
        policy_decisions=[{"policy": "affected_sources", "result": decision}],
    )


async def discover_affected_sources(
    *,
    record: MigrationRecord,
    candidate_store: CandidateStore,
    registry: SourceRegistry,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    identity_assurance: str = "operator_asserted",
) -> AffectedSources:
    """List the VIEW-authorized sources whose evidence supports an approval.

    Requires ontology_approver and a signed migration record. Every
    outcome is Article-12 audited before returning or raising.
    """
    started = time.perf_counter()

    async def emit(
        decision: AffectedSourcesDecision,
        sources: tuple[SourceRecord, ...] = (),
        registered_count: int = 0,
    ) -> None:
        await _audit_discovery(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
            decision=decision,
            sources=sources,
            registered_count=registered_count,
            started=started,
        )

    try:
        verify_signed_migration_record(record=record, audit=audit)
    except Exception:
        await emit("invalid_request")
        raise AffectedSourcesRequestError("Migration record is not a verified approval") from None

    if not subject.strip() or authz is None:
        await emit("deny")
        raise AffectedSourcesDeniedError("Affected-source discovery denied")

    try:
        collected = await _collect_authorized_approval_candidates(
            candidate_store=candidate_store,
            authz=authz,
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
        )
    except CandidateInspectionDeniedError:
        await emit("deny")
        raise AffectedSourcesDeniedError("Affected-source discovery denied") from None
    except Exception:
        # inspect_candidates has already audited the page-level cause.
        await emit("failure")
        raise

    matched = _matching(
        collected,
        ontology_id=record.ontology_id,
        pattern=evidence_pattern(record),
    )
    sources = {
        SourceRecord(
            source_id=stored.candidate.provenance.source_id,
            acl_ref=stored.candidate.acl_ref,
        )
        for stored in matched
    }

    registered: set[SourceRecord] = set()
    try:
        for source in sources:
            stored_record = await registry.get_source_record(
                source.source_id,
                acl_ref=source.acl_ref,
            )
            if stored_record == source:
                registered.add(source)
    except Exception:
        await emit("storage_error")
        raise

    # Permissions may have been revoked while the registry was read.
    try:
        await _recheck_approval_permissions(authz=authz, subject=subject, records=matched)
    except CandidateInspectionDeniedError:
        await emit("deny")
        raise AffectedSourcesDeniedError("Affected-source discovery denied") from None
    except Exception:
        await emit("authorization_error")
        raise

    result = AffectedSources(
        registered=_sorted_sources(registered),
        unregistered=_sorted_sources(sources - registered),
    )
    await emit(
        "allow",
        sources=result.registered + result.unregistered,
        registered_count=len(result.registered),
    )
    return result
