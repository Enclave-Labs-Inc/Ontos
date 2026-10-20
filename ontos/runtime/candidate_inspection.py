"""Audited, ACL-scoped inspection of rejected ontology candidates."""

from __future__ import annotations

import time

from ontos.audit.base import AuditSink
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, AuthzBackend
from ontos.extraction import RejectedCandidate
from ontos.runtime.models import AuditRecord
from ontos.storage.base import CandidateStore


class CandidateInspectionDenied(PermissionError):
    """The caller cannot inspect ontology candidates."""


async def inspect_candidates(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    limit: int = 100,
) -> tuple[list[RejectedCandidate], AuditRecord]:
    """Require reviewer + VIEW, then audit before returning.

    The caller's identity must be established by the transport layer;
    client-provided identity strings alone are not authentication.
    """
    started = time.perf_counter()

    async def emit(decision: str) -> AuditRecord:
        kwargs = dict(

            query_id=None,
            agent_identity=agent_identity,
            acting_on_behalf_of=subject,
            query_text="ontology candidate inspection",
            tool_invoked="candidates",
            tool_arguments={"limit": limit},
            result_fact_ids=[],
            latency_ms=(time.perf_counter() - started) * 1000,
            model_versions={"component": "candidate-inspection-m1"},
            policy_decisions=[
                {
                    "policy": "ontology_candidate_inspection",
                    "result": decision,
                }
            ],
        )
        emit_async = getattr(audit, "emit_async", None)
        if emit_async is not None:
            return await emit_async(**kwargs)
        return audit.emit(**kwargs)

    if not subject or authz is None:
        await emit("deny")
        raise CandidateInspectionDenied("Candidate inspection denied")

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        await emit("invalid_request")
        raise ValueError("limit must be between 1 and 100")

    try:
        is_reviewer = await authz.check(
            subject, ONTOLOGY_REVIEWER, "ontology"
        )
    except Exception:
        await emit("authorization_error")
        raise

    if not is_reviewer:
        await emit("deny")
        raise CandidateInspectionDenied("Candidate inspection denied")

    try:
        allowed_acls = await authz.list_authorized_objects(subject, VIEW)
    except Exception:
        await emit("authorization_error")
        raise

    try:
        candidates = await candidate_store.list_candidates(
            allowed_acls=allowed_acls,
            include_public=True,
            limit=limit,
        )
    except Exception:
        await emit("storage_error")
        raise

    record = await emit("allow")
    return candidates, record
