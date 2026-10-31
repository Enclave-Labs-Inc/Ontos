"""Authorized, audited, on-demand deterministic ontology suggestions."""

from __future__ import annotations

import time
from typing import Literal

import structlog

from ontos.audit.base import AuditSink
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, AuthzBackend
from ontos.authz.base import AuthzError
from ontos.ontology.loader import Ontology
from ontos.ontology.suggestion_backend import SuggestionBackend
from ontos.ontology.suggestion_pipeline import generate_llm_suggestions
from ontos.ontology.suggestion_trust import require_approved_suggestion_backend
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


async def _recheck_suggestion_permissions(
    *,
    authz: AuthzBackend | None,
    subject: str,
    records: list[StoredCandidate],
) -> None:
    """Fail closed if reviewer or source VIEW access was revoked."""
    if authz is None or not await authz.check(subject, ONTOLOGY_REVIEWER, "ontology"):
        raise CandidateInspectionDeniedError("Ontology suggestions denied")

    allowed_acls = set(await authz.list_authorized_objects(subject, VIEW))
    if any(
        item.candidate.acl_ref is not None and item.candidate.acl_ref not in allowed_acls
        for item in records
    ):
        raise CandidateInspectionDeniedError("Ontology suggestions denied")


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
    invalid_count: int = 0,
    generation_mode: Literal["m2a", "m2b"] = "m2a",
    generator_id: str | None = None,
    generator_version: str | None = None,
) -> AuditRecord:
    """Record a request without logging candidate text or raw IDs."""
    tool_arguments: dict[str, object] = {
        "identity_assurance": identity_assurance,
        "ontology_id": ontology.id,
        "ontology_version": ontology.version,
        "candidate_count": candidate_count,
        "suggestion_count": suggestion_count,
        "invalid_count": invalid_count,
    }
    policy_decisions = [{"policy": "ontology_suggestions", "result": decision}]
    model_versions = {"component": f"ontology-suggestions-{generation_mode}-v1"}
    if generation_mode == "m2b" and generator_id is not None and generator_version is not None:
        model_versions["generator_id"] = generator_id
        model_versions["generator_version"] = generator_version

    query_text = (
        "ontology deterministic suggestions"
        if generation_mode == "m2a"
        else "ontology llm-assisted suggestions"
    )

    from ontos.audit.postgres import PostgresAuditEmitter

    if isinstance(audit, PostgresAuditEmitter):
        return await audit.emit_async(
            query_id=None,
            agent_identity=agent_identity,
            acting_on_behalf_of=subject,
            query_text=query_text,
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
        query_text=query_text,
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
    backend: SuggestionBackend | None = None,
) -> tuple[list[OntologySuggestion], AuditRecord]:
    """Generate suggestions solely from the caller's VIEW-filtered candidates.

    M1 inspection enforces ontology_reviewer and VIEW on every page.
    A separate request-level audit covers generation, denial and failures.
    Suggestions are never persisted or written into the trusted graph.
    """
    started = time.perf_counter()
    records: list[StoredCandidate] = []
    invalid_count = 0
    audited_generator_id: str | None = None
    audited_generator_version: str | None = None
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
            generation_mode="m2b" if backend is not None else "m2a",
            decision="request_started",
            suggestion_refs=[],
            candidate_count=0,
            suggestion_count=0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

        # Reject unapproved backends before accessing candidate storage.
        if backend is not None:
            stage = "generation"
            approved = require_approved_suggestion_backend(backend)
            audited_generator_id = approved.id
            audited_generator_version = approved.version
            stage = "inspection"

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

        stage = "authorization"
        await _recheck_suggestion_permissions(
            authz=authz,
            subject=subject,
            records=records,
        )

        stage = "generation"
        reference_key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)

        # M2a always runs first, including when M2b is enabled.
        suggestions = suggest_missing_patterns(
            candidates=records,
            ontology=ontology,
            reference_key=reference_key,
        )

        if backend is not None:
            # Existing relation types are handled by M2a. Do not expose
            # those patterns to the model unnecessarily.
            llm_candidates = [
                record
                for record in records
                if ontology.relation_type(record.candidate.raw_triple.predicate) is None
            ]

            generated = await generate_llm_suggestions(
                authorized_candidates=llm_candidates,
                ontology=ontology,
                backend=backend,
                reference_key=reference_key,
            )
            suggestions.extend(generated.suggestions)
            invalid_count = generated.invalid_count

        # LLM generation can be slow; permissions may change meanwhile.
        # Recheck before auditing and returning any suggestion.
        stage = "authorization"
        await _recheck_suggestion_permissions(
            authz=authz,
            subject=subject,
            records=records,
        )

        refs = [ref for suggestion in suggestions for ref in suggestion.supporting_candidate_refs]

        stage = "audit"
        audit_record = await _audit_suggestions(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
            ontology=ontology,
            generation_mode="m2b" if backend is not None else "m2a",
            decision="allow",
            generator_id=audited_generator_id,
            generator_version=audited_generator_version,
            suggestion_refs=refs,
            candidate_count=len(records),
            suggestion_count=len(suggestions),
            invalid_count=invalid_count,
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
                generation_mode="m2b" if backend is not None else "m2a",
                decision=decision,
                generator_id=audited_generator_id,
                generator_version=audited_generator_version,
                suggestion_refs=[],
                candidate_count=len(records),
                suggestion_count=0,
                invalid_count=invalid_count,
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
