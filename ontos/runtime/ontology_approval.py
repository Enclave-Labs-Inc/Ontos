"""Audited candidate collection for operator-controlled ontology approvals.

This internal layer does not approve changes or write files.
The approval service must emit a request-level audit event before calling
it and audit all terminal outcomes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from uuid import UUID

from ontos.audit.base import AuditSink, emit_record
from ontos.authz import ONTOLOGY_APPROVER, VIEW, AuthzBackend
from ontos.migration.approval_audit import sign_migration_record
from ontos.migration.records import MigrationRecord
from ontos.ontology.approval_selection import (
    ApprovalChange,
    ApprovalSuggestionNotFoundError,
    RecomputedApproval,
    recompute_approval_change,
)
from ontos.ontology.artifact_writer import write_ontology_version
from ontos.ontology.loader import Ontology
from ontos.ontology.versioning import build_next_ontology
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDeniedError,
    candidate_audit_reference,
    inspect_candidates,
)
from ontos.runtime.models import AuditRecord
from ontos.storage.base import CandidateStore, StoredCandidate, candidate_storage_id

APPROVAL_PAGE_SIZE = 100
MAX_APPROVAL_CANDIDATES = 10_000


class ApprovalEvidenceLimitError(RuntimeError):
    """The candidate set exceeds the safe approval limit."""


class ApprovalEvidencePaginationError(RuntimeError):
    """Candidate pagination cannot safely produce a complete result."""


async def _recheck_approval_permissions(
    *,
    authz: AuthzBackend | None,
    subject: str,
    records: list[StoredCandidate],
) -> None:
    """Require an approver role and VIEW on all collected restricted evidence."""
    if authz is None or not await authz.check(subject, ONTOLOGY_APPROVER, "ontology"):
        raise CandidateInspectionDeniedError("Ontology approval denied")

    allowed_acls = set(await authz.list_authorized_objects(subject, VIEW))

    if any(
        record.candidate.acl_ref is not None and record.candidate.acl_ref not in allowed_acls
        for record in records
    ):
        raise CandidateInspectionDeniedError("Ontology approval denied")


async def _collect_authorized_approval_candidates(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    identity_assurance: str = "operator_asserted",
) -> list[StoredCandidate]:
    """Collect every VIEW-filtered page, then recheck all permissions.

    Each page is independently authorized and Article-12 audited by
    inspect_candidates. An incomplete collection never returns evidence.
    """
    records: list[StoredCandidate] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        page = await inspect_candidates(
            candidate_store=candidate_store,
            authz=authz,
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
            required_relation=ONTOLOGY_APPROVER,
            limit=APPROVAL_PAGE_SIZE,
            after=cursor,
            paginated=True,
        )

        records.extend(
            StoredCandidate(
                candidate=candidate,
                stored_id=candidate_storage_id(candidate),
            )
            for candidate in page.candidates
        )

        if len(records) > MAX_APPROVAL_CANDIDATES:
            raise ApprovalEvidenceLimitError("Approval candidate limit exceeded")

        next_cursor = page.next_cursor
        if next_cursor is None:
            break

        if next_cursor in seen_cursors or next_cursor == cursor:
            raise ApprovalEvidencePaginationError("Approval candidate pagination did not advance")

        seen_cursors.add(next_cursor)
        cursor = next_cursor

    await _recheck_approval_permissions(
        authz=authz,
        subject=subject,
        records=records,
    )

    return records


ApprovalAuditDecision = Literal[
    "request_started",
    "evidence_verified",
    "approval_granted",
    "published",
    "deny",
    "no_such_suggestion",
    "invalid_request",
    "authorization_error",
    "storage_error",
    "publication_failed",
    "failure",
]

_APPROVAL_CHANGE_REFERENCE_LABEL = b"ontology-approval-change-reference"
_APPROVAL_SCOPE_REFERENCE_LABEL = b"ontology-approval-scope-reference"


async def record_ontology_approval_event(
    *,
    audit: AuditSink,
    subject: str,
    agent_identity: str,
    ontology_id: str,
    base_version: str,
    new_version: str,
    requested_change: ApprovalChange,
    acl_ref: str | None,
    decision: ApprovalAuditDecision,
    identity_assurance: str = "operator_asserted",
    support_count: int = 0,
    supporting_candidate_refs: Sequence[str] = (),
    latency_ms: float = 0.0,
) -> AuditRecord:
    """Persist an Article-12 event without disclosing candidate content.

    The returned query_id identifies this actual persisted audit record.
    `approval_granted` records the approval decision, not publication.
    """
    canonical_change = json.dumps(
        requested_change.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )

    change_ref = candidate_audit_reference(
        audit.derive_key(_APPROVAL_CHANGE_REFERENCE_LABEL),
        canonical_change,
    )

    scope_ref = (
        candidate_audit_reference(
            audit.derive_key(_APPROVAL_SCOPE_REFERENCE_LABEL),
            acl_ref,
        )
        if acl_ref is not None
        else None
    )

    published_or_granted = decision in ("approval_granted", "published")

    refs = list(supporting_candidate_refs) if published_or_granted else []

    # Candidate references must already be keyed, opaque SHA-256 HMACs.
    # Never allow accidental raw candidate IDs into the approval audit.
    if any(len(ref) != 64 or any(char not in "0123456789abcdef" for char in ref) for ref in refs):
        raise ValueError("Approval audit requires opaque candidate references")

    return await emit_record(
        audit,
        query_id=None,
        agent_identity=agent_identity,
        acting_on_behalf_of=subject,
        query_text="ontology approval",
        tool_invoked="ontology_approve",
        tool_arguments={
            "identity_assurance": identity_assurance,
            "ontology_id": ontology_id,
            "base_version": base_version,
            "new_version": new_version,
            "change_kind": requested_change.action,
            "change_ref": change_ref,
            "scope_ref": scope_ref,
            "support_count": support_count if published_or_granted else 0,
        },
        result_fact_ids=refs,
        latency_ms=latency_ms,
        model_versions={"component": "ontology-approval-m3-2-v1"},
        policy_decisions=[{"policy": "ontology_approval", "result": decision}],
    )


async def _prepare_ontology_approval(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    ontology: Ontology,
    subject: str,
    agent_identity: str,
    requested_change: ApprovalChange,
    acl_ref: str | None,
    base_version: str,
    new_version: str,
    identity_assurance: str = "operator_asserted",
) -> RecomputedApproval:
    """Audit and recompute a change, without granting or publishing it.

    A subsequent publisher MUST recheck authorization and create its
    own persisted approval_granted and publication audit records.
    """

    async def audit_decision(
        decision: ApprovalAuditDecision,
    ) -> AuditRecord:
        return await record_ontology_approval_event(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            ontology_id=ontology.id,
            base_version=base_version,
            new_version=new_version,
            requested_change=requested_change,
            acl_ref=acl_ref,
            identity_assurance=identity_assurance,
            decision=decision,
        )

    # Fail closed: no candidate access before the request audit succeeds.
    await audit_decision("request_started")

    try:
        if (
            not base_version.strip()
            or not new_version.strip()
            or ontology.version != base_version
            or base_version == new_version
        ):
            raise ValueError("Invalid or stale ontology version")

        records = await _collect_authorized_approval_candidates(
            candidate_store=candidate_store,
            authz=authz,
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            identity_assurance=identity_assurance,
        )

        reference_key = audit.derive_key(b"candidate-audit-reference")

        result = recompute_approval_change(
            authorized_candidates=records,
            ontology=ontology,
            requested_change=requested_change,
            acl_ref=acl_ref,
            reference_key=reference_key,
        )

        # A revoked permission must invalidate recomputed evidence.
        await _recheck_approval_permissions(
            authz=authz,
            subject=subject,
            records=records,
        )

        # This is NOT approval. The caller must still publish safely.
        await audit_decision("evidence_verified")
        return result

    except BaseException as exc:
        if isinstance(exc, ApprovalSuggestionNotFoundError):
            decision: ApprovalAuditDecision = "no_such_suggestion"
        elif isinstance(exc, CandidateInspectionDeniedError):
            decision = "deny"
        elif isinstance(
            exc,
            (
                ValueError,
                ApprovalEvidenceLimitError,
                ApprovalEvidencePaginationError,
            ),
        ):
            decision = "invalid_request"
        else:
            decision = "failure"

        await audit_decision(decision)
        raise


async def approve_ontology_change(
    *,
    candidate_store: CandidateStore,
    authz: AuthzBackend | None,
    audit: AuditSink,
    ontology: Ontology,
    subject: str,
    agent_identity: str,
    requested_change: ApprovalChange,
    acl_ref: str | None,
    base_version: str,
    new_version: str,
    ontology_path: Path,
    migration_path: Path,
    identity_assurance: str = "operator_asserted",
) -> tuple[Ontology, MigrationRecord]:
    """Approve one evidence-backed change and write non-active YAML artifacts.

    The approval_query_id comes only from the persisted approval_granted
    event. The operator cannot supply it. This is a trusted-local-operator
    API, not identity authentication or automatic ontology activation.
    """
    selected = await _prepare_ontology_approval(
        candidate_store=candidate_store,
        authz=authz,
        audit=audit,
        ontology=ontology,
        subject=subject,
        agent_identity=agent_identity,
        requested_change=requested_change,
        acl_ref=acl_ref,
        base_version=base_version,
        new_version=new_version,
        identity_assurance=identity_assurance,
    )

    async def audit_outcome(
        decision: ApprovalAuditDecision,
        *,
        include_evidence: bool = False,
    ) -> AuditRecord:
        return await record_ontology_approval_event(
            audit=audit,
            subject=subject,
            agent_identity=agent_identity,
            ontology_id=ontology.id,
            base_version=base_version,
            new_version=new_version,
            requested_change=selected.change,
            acl_ref=selected.acl_ref,
            decision=decision,
            identity_assurance=identity_assurance,
            support_count=selected.support_count if include_evidence else 0,
            supporting_candidate_refs=(
                selected.supporting_candidate_refs if include_evidence else ()
            ),
        )

    async def verify_current_access() -> None:
        """Check approver role and selected evidence scope immediately."""
        if authz is None or not await authz.check(subject, ONTOLOGY_APPROVER, "ontology"):
            raise CandidateInspectionDeniedError("Ontology approval denied")

        allowed_acls = set(await authz.list_authorized_objects(subject, VIEW))
        if selected.acl_ref is not None and selected.acl_ref not in allowed_acls:
            raise CandidateInspectionDeniedError("Ontology approval denied")

    # Validate the additive transition before recording an approval.
    # The placeholder UUID is used for in-memory validation ONLY.
    # It is never published or returned as an approval identifier.
    try:
        preview = MigrationRecord(
            schema_version=1,
            ontology_id=ontology.id,
            from_version=base_version,
            to_version=new_version,
            migration_kind="identity",
            approval_query_id=UUID(int=0),
            approved_changes=[selected.change],
        )
        build_next_ontology(ontology, preview)

        if ontology_path.resolve() == migration_path.resolve():
            raise ValueError("Ontology and migration paths must differ")

        for target in (ontology_path, migration_path):
            if not target.parent.is_dir():
                raise FileNotFoundError(f"Destination directory does not exist: {target.parent}")
            if os.path.lexists(target):
                raise FileExistsError(f"Refusing to overwrite existing artifact: {target}")

        # Recheck before recording the approval decision.
        await verify_current_access()

    except BaseException as exc:
        if isinstance(exc, CandidateInspectionDeniedError):
            await audit_outcome("deny")
        elif isinstance(exc, (ValueError, FileNotFoundError, FileExistsError)):
            await audit_outcome("invalid_request")
        else:
            await audit_outcome("failure")
        raise

    # Persist approval BEFORE publishing either file.
    granted = await audit_outcome(
        "approval_granted",
        include_evidence=True,
    )

    # Bind the complete migration to the persisted approval event.
    # The signature survives audit retention pruning and is verified
    # independently when the migration is loaded for runtime reads.
    record = sign_migration_record(
        record=preview.model_copy(update={"approval_query_id": granted.query_id}),
        audit=audit,
    )

    try:
        # Permission changes during audit persistence must prevent files.
        await verify_current_access()
        updated = write_ontology_version(
            base=ontology,
            record=record,
            ontology_path=ontology_path,
            migration_path=migration_path,
        )
    except BaseException:
        await audit_outcome("publication_failed")
        raise

    # If the final publication audit fails, attempt to roll back only
    # our newly created files. Never blindly remove a replaced file.
    published_files: list[tuple[Path, int, int]] = []
    for target in (ontology_path, migration_path):
        try:
            stat = target.lstat()
            published_files.append((target, stat.st_dev, stat.st_ino))
        except OSError:
            pass

    try:
        await audit_outcome("published", include_evidence=True)
    except BaseException:
        for target, device, inode in reversed(published_files):
            try:
                current = target.lstat()
                if current.st_dev == device and current.st_ino == inode:
                    target.unlink()
            except OSError:
                pass
        raise

    return updated, record
