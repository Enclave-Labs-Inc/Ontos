"""Authorized, audited, on-demand deterministic ontology suggestions."""

from __future__ import annotations

import time
from typing import Literal

import structlog

from ontos.audit.base import AuditSink
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, AuthzBackend
from ontos.authz.base import AuthzError
from ontos.ontology.loader import Ontology
from ontos.ontology.suggestions import (
    OntologySuggestion,
    suggest_missing_patterns,
)
from ontos.runtime.candidate_inspection import (
    CANDIDATE_REFERENCE_KEY_LABEL,
    CandidateInspectionAuditError,
    CandidateInspectionDeniedError,
    inspect_candidates,
)
from ontos.runtime.models import AuditRecord
from ontos.storage.base import CandidateStore, StoredCandidate, candidate_storage_id

log = structlog.get_logger(__name__)

PAGE_SIZE = 100
MAX_CANDIDATES = 10_000


class SuggestionLimitExceededError(RuntimeError):
    """The authorized candidate set exceeds the supported request limit."""


class SuggestionPaginationError(RuntimeError):
    """Candidate pagination cannot safely produce a complete result."""


async def _audit_suggestions(
    *,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    identity_assurance: str,
    ontology: Ontology,
    decision: str,
    suggestion_refs: list[str],
    candidate_count: int,
    suggestion_count: int,
    latency_ms: float,
) -> AuditRecord:
    """Record a request without logging candidate text or raw IDs."""
    tool_arguments: dict[str, object] = {
        "identity_assurance": identity_assurance,
        "ontology_id": ontology.id,
        "ontology_version": ontology.version,
        "candidate_count": candidate_count,
        "suggestion_count": suggestion_count,
    }
    policy_decisions = [{"policy": "ontology_suggestions", "result": decision}]
    model_versions = {"component": "ontology-suggestions-m2a-v1"}

    from ontos.audit.postgres import PostgresAuditEmitter

    if isinstance(audit, PostgresAuditEmitter):
        return await audit.emit_async(
            query_id=None,
            agent_identity=agent_identity,
            acting_on_behalf_of=subject,
            query_text="ontology deterministic suggestions",
            tool_invoked="ontology_suggestions",
            tool_arguments=tool_arguments,
            result_fact_ids=suggestion_refs,
            latency_ms=latency_ms,
            model_versions=model_versions,
            policy_decisions=policy_decisions,
        )

    return audit.emit(
        query_id=None,
        agent_identity=agent_identity,
        acting_on_behalf_of=subject,
        query_text="ontology deterministic suggestions",
        tool_invoked="ontology_suggestions",
        tool_arguments=tool_arguments,
        result_fact_ids=suggestion_refs,
        latency_ms=latency_ms,
        model_versions=model_versions,
        policy_decisions=policy_decisions,
    )


async def list_ontology_suggestions(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    ontology: Ontology,
    subject: str,
    agent_identity: str,
    identity_assurance: str = "unverified",
) -> tuple[list[OntologySuggestion], AuditRecord]:
    """Generate suggestions solely from the caller's VIEW-filtered candidates.

    M1 inspection enforces ontology_reviewer and VIEW on every page.
    A separate request-level audit covers generation, denial and failures.
    Suggestions are never persisted or written into the trusted graph.
    """
    started = time.perf_counter()
    records: list[StoredCandidate] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    stage = "inspection"

    decision: Literal[
        "allow",
        "deny",
        "authorization_error",
        "storage_error",
        "audit_error",
        "incomplete_result",
        "generation_error",
    ] = "allow"

    try:
        # Fail closed: no candidate reads before the audit sink has
        # durably accepted the suggestions request.
        await _audit_suggestions(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
            ontology=ontology,
            decision="request_started",
            suggestion_refs=[],
            candidate_count=0,
            suggestion_count=0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

        while True:
            page = await inspect_candidates(
                candidate_store=candidate_store,
                authz=authz,
                audit=audit,
                subject=subject,
                agent_identity=agent_identity,
                limit=PAGE_SIZE,
                after=cursor,
                identity_assurance=identity_assurance,
                paginated=True,
            )

            for candidate in page.candidates:
                records.append(
                    StoredCandidate(
                        candidate=candidate,
                        stored_id=candidate_storage_id(candidate),
                    )
                )

            if len(records) > MAX_CANDIDATES:
                raise SuggestionLimitExceededError("Suggestion candidate limit exceeded")

            if page.next_cursor is None:
                break

            if page.next_cursor in seen_cursors:
                raise SuggestionPaginationError("Candidate pagination did not advance")

            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

        # Revalidate permissions before using the collected evidence.
        # Rechecking the reviewer role alone is insufficient: VIEW access
        # may have been revoked while candidate pages were retrieved.
        stage = "authorization"

        # A revocation here is a denial, not a backend fault (#85).
        if authz is None or not await authz.check(subject, ONTOLOGY_REVIEWER, "ontology"):
            raise CandidateInspectionDeniedError("Ontology suggestions denied")

        current_allowed_acls = set(await authz.list_authorized_objects(subject, VIEW))

        # Re-check each previously visible restricted candidate against
        # the caller's current VIEW permissions.
        if any(
            item.candidate.acl_ref is not None
            and item.candidate.acl_ref not in current_allowed_acls
            for item in records
        ):
            raise CandidateInspectionDeniedError("Ontology suggestions denied")

        stage = "generation"
        reference_key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)

        suggestions = suggest_missing_patterns(
            candidates=records,
            ontology=ontology,
            reference_key=reference_key,
        )

        refs = [ref for suggestion in suggestions for ref in suggestion.supporting_candidate_refs]

        stage = "audit"
        audit_record = await _audit_suggestions(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
            ontology=ontology,
            decision="allow",
            suggestion_refs=refs,
            candidate_count=len(records),
            suggestion_count=len(suggestions),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

        return suggestions, audit_record

    except Exception as exc:
        if isinstance(exc, CandidateInspectionAuditError):
            decision = "audit_error"
        elif isinstance(exc, CandidateInspectionDeniedError):
            decision = "deny"
        elif isinstance(exc, PermissionError):
            decision = "authorization_error"
        elif isinstance(exc, (SuggestionLimitExceededError, SuggestionPaginationError)):
            decision = "incomplete_result"
        elif isinstance(exc, AuthzError) or stage == "authorization":
            decision = "authorization_error"
        elif stage == "audit":
            decision = "audit_error"
        elif stage == "generation":
            decision = "generation_error"
        else:
            decision = "storage_error"

        try:
            await _audit_suggestions(
                audit=audit,
                subject=subject,
                agent_identity=agent_identity,
                identity_assurance=identity_assurance,
                ontology=ontology,
                decision=decision,
                suggestion_refs=[],
                candidate_count=len(records),
                suggestion_count=0,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as audit_exc:
            # The request must still fail with the original error, but the
            # trail now ends at `request_started` with no outcome; record
            # why for operators. Types only: messages can carry backend detail.
            log.error(
                "ontology-suggestions-failure-audit-failed",
                error_type=type(audit_exc).__name__,
                original_error_type=type(exc).__name__,
            )
            raise exc from None
        raise


async def audit_suggestion_setup_failure(
    *,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    ontology: Ontology,
    decision: Literal["authorization_error", "storage_error"],
    identity_assurance: str = "operator_asserted",
) -> AuditRecord:
    """Audit setup failures before the suggestions service can run."""
    return await _audit_suggestions(
        audit=audit,
        subject=subject,
        agent_identity=agent_identity,
        identity_assurance=identity_assurance,
        ontology=ontology,
        decision=decision,
        suggestion_refs=[],
        candidate_count=0,
        suggestion_count=0,
        latency_ms=0.0,
    )
