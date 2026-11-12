"""M3-3: re-ingestion authorization and Article-12 privacy."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    authorize_scoped_reingest,
)
from ontos.storage.sources import SourceRecord

SUBJECT = "user:operator"
SECRET_SOURCE = SourceRecord(
    "secret-finance-document.txt",
    "acl:finance-sensitive",
)


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-3-test-audit-key")


def _authz(*, role: bool = True, view: bool = True) -> InMemoryAuthz:
    backend = InMemoryAuthz()
    if role:
        backend.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    if view:
        backend.grant(SUBJECT, VIEW, "acl:finance-sensitive")
    return backend


def _decision(audit: AuditEmitter) -> str:
    record = audit._chain[-1]
    decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
    return decisions[0]["result"]


@pytest.mark.asyncio
async def test_authorized_restricted_scope_is_audited() -> None:
    audit = _audit()

    await authorize_scoped_reingest(
        authz=_authz(),
        audit=audit,
        subject=SUBJECT,
        sources=[SECRET_SOURCE],
    )

    assert len(audit) == 1
    assert _decision(audit) == "allow"
    assert audit.verify_chain()

    serialized = audit._chain[-1].model_dump_json()
    assert "secret-finance-document.txt" not in serialized
    assert "acl:finance-sensitive" not in serialized

    arguments = json.loads(audit._chain[-1].article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert len(arguments["scope_ref"]) == 64
    assert arguments["requested_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "view"),
    [(False, True), (True, False), (False, False)],
)
async def test_missing_permissions_fail_closed(
    role: bool,
    view: bool,
) -> None:
    audit = _audit()

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=_authz(role=role, view=view),
            audit=audit,
            subject=SUBJECT,
            sources=[SECRET_SOURCE],
        )

    assert len(audit) == 1
    assert _decision(audit) == "deny"
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_public_scope_still_requires_operator_role() -> None:
    audit = _audit()
    public = SourceRecord("public.txt", None)

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=_authz(role=False),
            audit=audit,
            subject=SUBJECT,
            sources=[public],
        )

    assert _decision(audit) == "deny"

    allowed_audit = _audit()
    await authorize_scoped_reingest(
        authz=_authz(role=True, view=False),
        audit=allowed_audit,
        subject=SUBJECT,
        sources=[public],
    )
    assert _decision(allowed_audit) == "allow"


@pytest.mark.asyncio
async def test_missing_authz_backend_denied_and_audited() -> None:
    audit = _audit()

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=None,
            audit=audit,
            subject=SUBJECT,
            sources=[SECRET_SOURCE],
        )

    assert _decision(audit) == "deny"


@pytest.mark.asyncio
async def test_permission_revocation_blocks_recheck() -> None:
    backend = _authz()
    audit = _audit()

    await authorize_scoped_reingest(
        authz=backend,
        audit=audit,
        subject=SUBJECT,
        sources=[SECRET_SOURCE],
    )

    backend.revoke(SUBJECT, VIEW, "acl:finance-sensitive")

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=backend,
            audit=audit,
            subject=SUBJECT,
            sources=[SECRET_SOURCE],
        )

    assert len(audit) == 2
    assert _decision(audit) == "deny"


@pytest.mark.asyncio
async def test_unavailable_authorization_is_denied_and_audited() -> None:
    class BrokenAuthz(InMemoryAuthz):
        async def check(self, subject: str, relation: str, object_: str) -> bool:
            raise RuntimeError("sensitive backend error")

    audit = _audit()

    with pytest.raises(
        ScopedReingestDeniedError,
        match="Scoped re-ingestion denied",
    ):
        await authorize_scoped_reingest(
            authz=BrokenAuthz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SECRET_SOURCE],
        )

    assert _decision(audit) == "authorization_error"
    assert "sensitive backend error" not in audit._chain[-1].model_dump_json()


@pytest.mark.asyncio
async def test_audit_failure_prevents_authorization() -> None:
    class BrokenAudit(AuditEmitter):
        def emit(self, **kwargs: object):
            raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await authorize_scoped_reingest(
            authz=_authz(),
            audit=BrokenAudit(signing_key=b"audit-test-key"),
            subject=SUBJECT,
            sources=[SECRET_SOURCE],
        )


@pytest.mark.asyncio
async def test_invalid_scope_is_denied_before_authz() -> None:
    audit = _audit()

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SECRET_SOURCE, SECRET_SOURCE],
        )

    assert _decision(audit) == "invalid_request"
