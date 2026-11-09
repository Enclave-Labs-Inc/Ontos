"""M3-2: forged and mismatched migration approvals fail closed."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.migration.approval_audit import (
    MigrationApprovalAuditError,
    sign_migration_record,
    verify_migration_approval_records,
    verify_signed_migration_record,
)
from ontos.migration.records import (
    AddPatternChange,
    MigrationRecord,
)
from ontos.runtime.ontology_approval import record_ontology_approval_event


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

    event = await record_ontology_approval_event(
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

    record = MigrationRecord(
        schema_version=1,
        ontology_id="ontos.starter",
        from_version="0.1",
        to_version="0.2",
        migration_kind="identity",
        approval_query_id=event.query_id,
        approved_changes=[_change()],
    )
    return (
        sign_migration_record(record=record, audit=audit)
        if decision == "approval_granted"
        else record
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

    with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
        await verify_migration_approval_records(
            records=[forged],
            audit=audit,
        )


@pytest.mark.parametrize(
    "decision",
    ["request_started", "evidence_verified", "published", "deny"],
)
async def test_unsigned_nonapproval_events_do_not_authorize_migration(
    decision: str,
) -> None:
    audit = _audit()
    record = await _record(audit, decision=decision)

    with pytest.raises(MigrationApprovalAuditError, match="signature missing"):
        await verify_migration_approval_records(
            records=[record],
            audit=audit,
        )


async def test_mismatched_version_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    altered = record.model_copy(update={"to_version": "0.3"})

    with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
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

    with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
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


async def test_runtime_signature_verification_does_not_scan_audit_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()
    record = await _record(audit)

    monkeypatch.setattr(audit, "verify_chain", lambda: False)

    # Signed migration verification does not consult the audit chain.
    await verify_migration_approval_records(
        records=[record],
        audit=audit,
    )


async def test_signed_migration_verifies_without_audit_history() -> None:
    audit = _audit()
    record = sign_migration_record(record=await _record(audit), audit=audit)

    assert record.approval_signature is not None
    assert len(record.approval_signature) == 64

    # A fresh emitter has the same signing key but no approval events.
    # This simulates verification after historical audit retention pruning.
    fresh_audit = _audit()
    assert len(fresh_audit) == 0
    verify_signed_migration_record(record=record, audit=fresh_audit)


async def test_unsigned_migration_is_rejected() -> None:
    audit = _audit()
    record = await _record(audit)
    record = record.model_copy(update={"approval_signature": None})

    with pytest.raises(MigrationApprovalAuditError, match="signature missing"):
        verify_signed_migration_record(record=record, audit=audit)


async def test_signed_migration_rejects_changed_approval_id() -> None:
    audit = _audit()
    record = sign_migration_record(record=await _record(audit), audit=audit)
    tampered = record.model_copy(update={"approval_query_id": uuid4()})

    with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
        verify_signed_migration_record(record=tampered, audit=audit)


async def test_signed_migration_rejects_changed_versions_and_change() -> None:
    audit = _audit()
    record = sign_migration_record(record=await _record(audit), audit=audit)

    altered_version = record.model_copy(update={"to_version": "0.9"})
    altered_change = record.model_copy(
        update={
            "approved_changes": [
                AddPatternChange(
                    action="add_pattern",
                    subject_type="Person",
                    predicate="subsidiary_of",
                    object_type="Company",
                )
            ]
        }
    )

    for tampered in (altered_version, altered_change):
        with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
            verify_signed_migration_record(record=tampered, audit=audit)


async def test_signed_migration_rejects_wrong_signing_key() -> None:
    audit = _audit()
    record = sign_migration_record(record=await _record(audit), audit=audit)
    wrong_audit = AuditEmitter(b"different-migration-signing-key")

    with pytest.raises(MigrationApprovalAuditError, match="signature verification failed"):
        verify_signed_migration_record(record=record, audit=wrong_audit)


async def test_runtime_verification_survives_deleted_approval_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_audit = _audit()
    record = sign_migration_record(
        record=await _record(original_audit),
        audit=original_audit,
    )

    # Same signing key, no audit events: represents pruned history.
    fresh_audit = _audit()
    assert len(fresh_audit) == 0

    def forbidden_audit_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Runtime must not read or scan audit history")

    monkeypatch.setattr(fresh_audit, "verify_chain", forbidden_audit_read)
    monkeypatch.setattr(fresh_audit, "by_query_id", forbidden_audit_read)

    await verify_migration_approval_records(
        records=[record],
        audit=fresh_audit,
    )


async def test_runtime_verification_rejects_unsigned_records() -> None:
    audit = _audit()
    record = await _record(audit)
    record = record.model_copy(update={"approval_signature": None})

    with pytest.raises(MigrationApprovalAuditError, match="signature missing"):
        await verify_migration_approval_records(
            records=[record],
            audit=audit,
        )


async def test_runtime_verification_rejects_tampered_signed_record() -> None:
    audit = _audit()
    record = sign_migration_record(
        record=await _record(audit),
        audit=audit,
    )
    tampered = record.model_copy(update={"approval_query_id": uuid4()})

    with pytest.raises(
        MigrationApprovalAuditError,
        match="signature verification failed",
    ):
        await verify_migration_approval_records(
            records=[tampered],
            audit=audit,
        )
