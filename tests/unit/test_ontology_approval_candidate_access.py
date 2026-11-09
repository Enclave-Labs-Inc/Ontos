"""Approval evidence uses an independent role and current VIEW rights."""

from unittest.mock import AsyncMock, Mock

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import (
    ONTOLOGY_APPROVER,
    ONTOLOGY_REVIEWER,
    VIEW,
    InMemoryAuthz,
)
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDeniedError,
    CandidateInspectionRequestError,
    inspect_candidates,
)


def make_store() -> Mock:
    store = Mock()
    store.list_candidate_records = AsyncMock(return_value=[])
    return store


def make_audit() -> AuditEmitter:
    return AuditEmitter(b"ontology-approval-test-signing-key")


async def test_approver_can_inspect_without_reviewer_role() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    authz.grant("user:approver", VIEW, "acl:legal")
    store = make_store()
    audit = make_audit()

    candidates, record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
        required_relation=ONTOLOGY_APPROVER,
    )

    assert candidates == []
    assert record.query_id is not None
    assert audit.verify_chain()
    kwargs = store.list_candidate_records.await_args.kwargs
    assert kwargs["allowed_acls"] == ["acl:legal"]
    assert kwargs["include_public"] is True


async def test_reviewer_cannot_use_approval_access() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    store = make_store()
    audit = make_audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="test",
            required_relation=ONTOLOGY_APPROVER,
        )

    store.list_candidate_records.assert_not_awaited()
    assert audit.verify_chain()


async def test_approver_cannot_use_default_reviewer_access() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    store = make_store()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=make_audit(),
            subject="user:approver",
            agent_identity="test",
        )

    store.list_candidate_records.assert_not_awaited()


async def test_view_revocation_during_approval_read_fails_closed() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    authz.grant("user:approver", VIEW, "acl:legal")
    store = make_store()
    audit = make_audit()

    async def revoke_during_read(**kwargs: object) -> list[object]:
        assert kwargs["allowed_acls"] == ["acl:legal"]
        authz.revoke("user:approver", VIEW, "acl:legal")
        return []

    store.list_candidate_records.side_effect = revoke_during_read

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:approver",
            agent_identity="test",
            required_relation=ONTOLOGY_APPROVER,
        )

    assert audit.verify_chain()


async def test_unknown_access_relation_is_rejected_before_storage() -> None:
    authz = InMemoryAuthz()
    store = make_store()
    audit = make_audit()

    with pytest.raises(CandidateInspectionRequestError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:approver",
            agent_identity="test",
            required_relation="audit_exporter",
        )

    store.list_candidate_records.assert_not_awaited()
    assert audit.verify_chain()
