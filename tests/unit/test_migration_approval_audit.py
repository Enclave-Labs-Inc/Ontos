"""M3-2: forged and mismatched migration approvals fail closed."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.migration.approval_audit import (
    MigrationApprovalAuditError,
    verify_migration_approval_records,
)
from ontos.migration.records import (
    AddPatternChange,
    MigrationRecord,
)
from ontos.runtime.ontology_approval import _audit_approval


def _change() -> AddPatternChange:
    return AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )


def _audit() -> AuditEmitter:
    return AuditEmitter(b"m3-2-migration-approval-verification-test")


async def _record(
    audit: AuditEmitter,
    *,
    decision: str = "approval_granted",
) -> MigrationRecord:
    from typing import cast

    from ontos.runtime.ontology_approval import ApprovalAuditDecision

    event = await _audit_approval(
        audit=audit,
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        ontology_id="ontos.starter",
        base_version="0.1",
        new_version="0.2",
        requested_change=_change(),
        acl_ref="acl:legal",
        decision=cast(ApprovalAuditDecision, decision),
        supporting_candidate_refs=["a" * 64],
        support_count=1,
    )

    return MigrationRecord(
        schema_version=1,
        ontology_id="ontos.starter",
        from_version="0.1",
        to_version="0.2",
        migration_kind="identity",
        approval_query_id=event.query_id,
        approved_changes=[_change()],
    )


async def test_genuine_matching_approval_passes() -> None:
    audit = _audit()
    record = await _record(audit)

    await verify_migration_approval_records(
        records=[record],
        audit=audit,
    )


async def test_unknown_approval_id_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    forged = record.model_copy(update={"approval_query_id": uuid4()})

    with pytest.raises(MigrationApprovalAuditError, match="not found"):
        await verify_migration_approval_records(
            records=[forged],
            audit=audit,
        )


@pytest.mark.parametrize(
    "decision",
    ["request_started", "evidence_verified", "published", "deny"],
)
async def test_nonapproval_audit_events_are_rejected(
    decision: str,
) -> None:
    audit = _audit()
    record = await _record(audit, decision=decision)

    with pytest.raises(MigrationApprovalAuditError, match="not a valid ontology approval"):
        await verify_migration_approval_records(
            records=[record],
            audit=audit,
        )


async def test_mismatched_version_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    altered = record.model_copy(update={"to_version": "0.3"})

    with pytest.raises(MigrationApprovalAuditError, match="does not match"):
        await verify_migration_approval_records(
            records=[altered],
            audit=audit,
        )


async def test_mismatched_change_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    different_change = AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="subsidiary_of",
        object_type="Company",
    )
    altered = record.model_copy(update={"approved_changes": [different_change]})

    with pytest.raises(MigrationApprovalAuditError, match="does not match"):
        await verify_migration_approval_records(
            records=[altered],
            audit=audit,
        )


async def test_multi_change_reuse_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    altered = record.model_copy(update={"approved_changes": [_change(), _change()]})

    with pytest.raises(MigrationApprovalAuditError, match="exactly one change"):
        await verify_migration_approval_records(
            records=[altered],
            audit=audit,
        )


async def test_failed_audit_chain_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()
    record = await _record(audit)

    monkeypatch.setattr(audit, "verify_chain", lambda: False)

    with pytest.raises(MigrationApprovalAuditError, match="chain failed"):
        await verify_migration_approval_records(
            records=[record],
            audit=audit,
        )
