"""M3-3: authorization must precede scoped source loading."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.ingest.base import SourceDocument
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    prepare_scoped_reingest,
)
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

SUBJECT = "user:operator"
SOURCE = SourceRecord("sensitive.txt", "acl:finance")


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-3-preparation-test-key")


def _authz(
    *,
    role: bool = True,
    view: bool = True,
) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    if role:
        authz.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    if view:
        authz.grant(SUBJECT, VIEW, "acl:finance")
    return authz


def _decisions(audit: AuditEmitter) -> list[str]:
    decisions = []
    for record in audit._chain:
        policy = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        decisions.append(policy[0]["result"])
    return decisions


def _document(
    source: SourceRecord = SOURCE,
) -> SourceDocument:
    return SourceDocument(
        source_id=source.source_id,
        text="Sensitive source content",
        acl_ref=source.acl_ref,
    )


@pytest.mark.asyncio
async def test_authorized_source_loaded_after_registry_validation() -> None:
    store = NetworkxStore()
    await store.register_source(SOURCE)

    calls: list[SourceRecord] = []
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        calls.append(identity)
        return _document(identity)

    selected = await prepare_scoped_reingest(
        authz=_authz(),
        audit=audit,
        subject=SUBJECT,
        sources=[SOURCE],
        registry=store,
        load_source=loader,
    )

    assert selected == [_document()]
    assert calls == [SOURCE]
    assert _decisions(audit) == ["allow", "allow"]
    assert audit.verify_chain()

    serialized = json.dumps([record.model_dump(mode="json") for record in audit._chain])
    assert "sensitive.txt" not in serialized
    assert "acl:finance" not in serialized
    assert "Sensitive source content" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "view"),
    [
        (False, True),
        (True, False),
        (False, False),
    ],
)
async def test_denied_operator_cannot_load_source(
    role: bool,
    view: bool,
) -> None:
    store = NetworkxStore()
    await store.register_source(SOURCE)
    calls: list[SourceRecord] = []
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        calls.append(identity)
        return _document(identity)

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=_authz(role=role, view=view),
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert calls == []
    assert _decisions(audit) == ["deny"]


@pytest.mark.asyncio
async def test_unregistered_source_never_loaded() -> None:
    store = NetworkxStore()
    calls: list[SourceRecord] = []
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        calls.append(identity)
        return _document(identity)

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert calls == []
    assert _decisions(audit) == ["allow", "scope_unavailable"]


@pytest.mark.asyncio
async def test_wrong_registered_acl_never_loaded() -> None:
    store = NetworkxStore()
    await store.register_source(SourceRecord("sensitive.txt", "acl:other"))
    calls: list[SourceRecord] = []
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        calls.append(identity)
        return _document(identity)

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert calls == []
    assert _decisions(audit)[-1] == "scope_unavailable"


@pytest.mark.asyncio
async def test_loader_failure_is_sanitized_and_audited() -> None:
    store = NetworkxStore()
    await store.register_source(SOURCE)
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        raise RuntimeError("private path and secret document contents")

    with pytest.raises(
        ScopedReingestDeniedError,
        match="Scoped source loading failed",
    ):
        await prepare_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert _decisions(audit)[-1] == "source_unavailable"
    assert "secret document contents" not in json.dumps(
        [record.model_dump(mode="json") for record in audit._chain]
    )


@pytest.mark.asyncio
async def test_connector_acl_mismatch_fails_preflight() -> None:
    store = NetworkxStore()
    await store.register_source(SOURCE)
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        return _document(SourceRecord(identity.source_id, "acl:other"))

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert _decisions(audit)[-1] == "preflight_failed"


@pytest.mark.asyncio
async def test_revoked_access_blocks_returning_documents() -> None:
    store = NetworkxStore()
    await store.register_source(SOURCE)

    authz = _authz()
    audit = _audit()

    async def loader(identity: SourceRecord) -> SourceDocument:
        authz.revoke(SUBJECT, VIEW, "acl:finance")
        return _document(identity)

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=authz,
            audit=audit,
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert _decisions(audit) == ["allow", "deny"]
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_missing_audit_prevents_source_loading() -> None:
    class BrokenAudit(AuditEmitter):
        def emit(self, **kwargs: object):
            raise RuntimeError("audit unavailable")

    store = NetworkxStore()
    await store.register_source(SOURCE)

    calls: list[SourceRecord] = []

    async def loader(identity: SourceRecord) -> SourceDocument:
        calls.append(identity)
        return _document(identity)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await prepare_scoped_reingest(
            authz=_authz(),
            audit=BrokenAudit(signing_key=b"audit-test-key"),
            subject=SUBJECT,
            sources=[SOURCE],
            registry=store,
            load_source=loader,
        )

    assert calls == []
