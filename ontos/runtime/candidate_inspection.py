"""Audited, ACL-scoped inspection of rejected ontology candidates."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Literal, overload

from ontos.audit.base import AuditSink
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, AuthzBackend
from ontos.extraction import RejectedCandidate
from ontos.runtime.models import AuditRecord
from ontos.storage.base import (
    CandidateStore,
    InvalidCandidateCursorError,
)


@dataclass(frozen=True)
class CandidateInspectionPage:
    """Authorized candidate page with its persisted audit record."""

    candidates: list[RejectedCandidate]
    next_cursor: str | None
    audit_record: AuditRecord


CANDIDATE_REFERENCE_KEY_LABEL = b"candidate-audit-reference"


def candidate_audit_reference(reference_key: bytes, value: str) -> str:
    """Keyed reference to a candidate id or cursor for the audit log.

    Candidate ids and cursors are plain hashes of guessable content
    (source, entities, predicate). Logging them directly would let an
    audit reader confirm guessed restricted triples offline (#72). A keyed
    HMAC keeps records traceable for whoever holds the deployment's audit
    key, via `audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)`, and opaque
    to everyone else.
    """
    return hmac.new(reference_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


class CandidateInspectionRequestError(ValueError):
    """The inspection request itself is invalid (limit out of range)."""


async def _emit_inspection_record(
    audit: AuditSink,
    *,
    agent_identity: str,
    subject: str,
    limit: object,
    identity_assurance: str,
    after_ref: str | None,
    decision: str,
    disclosed_refs: list[str],
    latency_ms: float,
) -> AuditRecord:
    tool_arguments: dict[str, object] = {
        "limit": limit,
        "identity_assurance": identity_assurance,
        "after_cursor_ref": after_ref,
    }
    policy_decisions = [{"policy": "ontology_candidate_inspection", "result": decision}]
    model_versions = {"component": "candidate-inspection-m1"}
    from ontos.audit.postgres import PostgresAuditEmitter

    if isinstance(audit, PostgresAuditEmitter):
        return await audit.emit_async(
            query_id=None,
            agent_identity=agent_identity,
            acting_on_behalf_of=subject,
            query_text="ontology candidate inspection",
            tool_invoked="candidates",
            tool_arguments=tool_arguments,
            result_fact_ids=disclosed_refs,
            latency_ms=latency_ms,
            model_versions=model_versions,
            policy_decisions=policy_decisions,
        )
    return audit.emit(
        query_id=None,
        agent_identity=agent_identity,
        acting_on_behalf_of=subject,
        query_text="ontology candidate inspection",
        tool_invoked="candidates",
        tool_arguments=tool_arguments,
        result_fact_ids=disclosed_refs,
        latency_ms=latency_ms,
        model_versions=model_versions,
        policy_decisions=policy_decisions,
    )


def _cursor_reference(audit: AuditSink, after: object) -> str | None:
    if not isinstance(after, str):
        return None
    return candidate_audit_reference(audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL), after)


async def audit_inspection_failure(
    *,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    decision: Literal["authorization_error", "storage_error"],
    limit: object,
    after: str | None,
    identity_assurance: str,
) -> AuditRecord:
    """Audit an inspection attempt that failed before `inspect_candidates`.

    Callers that build the authz backend or store themselves (the CLI)
    use this so setup failures still leave an Article-12 record.
    """
    return await _emit_inspection_record(
        audit,
        agent_identity=agent_identity,
        subject=subject,
        limit=limit,
        identity_assurance=identity_assurance,
        after_ref=_cursor_reference(audit, after),
        decision=decision,
        disclosed_refs=[],
        latency_ms=0.0,
    )


class CandidateInspectionDeniedError(PermissionError):
    """The caller cannot inspect ontology candidates."""


@overload
async def inspect_candidates(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    limit: int = 100,
    after: str | None = None,
    identity_assurance: str = "unverified",
    paginated: Literal[True],
) -> CandidateInspectionPage: ...


@overload
async def inspect_candidates(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    limit: int = 100,
    after: str | None = None,
    identity_assurance: str = "unverified",
    paginated: Literal[False] = False,
) -> tuple[list[RejectedCandidate], AuditRecord]: ...


async def inspect_candidates(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    limit: int = 100,
    after: str | None = None,
    identity_assurance: str = "unverified",
    paginated: bool = False,
) -> tuple[list[RejectedCandidate], AuditRecord] | CandidateInspectionPage:
    """Require reviewer + VIEW, then audit before returning.

    The caller's identity must be established by the transport layer;
    client-provided identity strings alone are not authentication.
    """
    started = time.perf_counter()

    reference_key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
    cursor_reference = _cursor_reference(audit, after)

    async def emit(decision: str, disclosed_ids: list[str] | None = None) -> AuditRecord:
        return await _emit_inspection_record(
            audit,
            agent_identity=agent_identity,
            subject=subject,
            limit=limit,
            identity_assurance=identity_assurance,
            after_ref=cursor_reference,
            decision=decision,
            disclosed_refs=disclosed_ids or [],
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    if not subject or authz is None:
        await emit("deny")
        raise CandidateInspectionDeniedError("Candidate inspection denied")

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        await emit("invalid_request")
        raise CandidateInspectionRequestError("limit must be between 1 and 100")

    try:
        is_reviewer = await authz.check(subject, ONTOLOGY_REVIEWER, "ontology")
    except Exception:
        await emit("authorization_error")
        raise

    if not is_reviewer:
        await emit("deny")
        raise CandidateInspectionDeniedError("Candidate inspection denied")

    try:
        allowed_acls = await authz.list_authorized_objects(subject, VIEW)
    except Exception:
        await emit("authorization_error")
        raise

    try:
        stored_records = await candidate_store.list_candidate_records(
            allowed_acls=allowed_acls,
            include_public=True,
            limit=limit + 1,
            after=after,
        )
    except InvalidCandidateCursorError:
        await emit("invalid_request")
        raise
    except Exception:
        await emit("storage_error")
        raise

    # Fetching one extra candidate detects whether another page exists.
    # Never disclose or audit the lookahead candidate.
    visible_records = stored_records[:limit]
    visible_page = [item.candidate for item in visible_records]
    disclosed_ids = [
        candidate_audit_reference(reference_key, item.stored_id) for item in visible_records
    ]
    record = await emit("allow", disclosed_ids)

    if paginated:
        from ontos.storage.base import encode_candidate_cursor

        next_cursor = (
            encode_candidate_cursor(visible_page[-1])
            if len(stored_records) > limit and visible_page
            else None
        )
        return CandidateInspectionPage(
            candidates=visible_page,
            next_cursor=next_cursor,
            audit_record=record,
        )

    return visible_page, record
