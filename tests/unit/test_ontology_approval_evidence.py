"""M3-2: complete, permission-aware evidence collection."""

from __future__ import annotations

import pytest

import ontos.runtime.ontology_approval as approval
from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
from ontos.storage.base import candidate_storage_id
from ontos.storage.networkx_store import NetworkxStore


def _candidate(source: str, acl: str | None) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id=f"person-{source}",
            subject_type="Person",
            subject_canonical_name=f"Person {source}",
            predicate="acquired",
            object_id=f"company-{source}",
            object_type="Company",
            object_canonical_name=f"Company {source}",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id=source,
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.2",
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )


def _authz() -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    authz.grant("user:approver", VIEW, "acl:legal")
    return authz


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-2-evidence-collection-test-key")


async def _collect(
    store: NetworkxStore,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
):
    return await approval._collect_authorized_approval_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
    )


async def test_real_store_collects_every_visible_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval, "APPROVAL_PAGE_SIZE", 2)

    store = NetworkxStore()
    visible = [
        _candidate("public", None),
        _candidate("legal-a", "acl:legal"),
        _candidate("legal-b", "acl:legal"),
    ]
    hidden = _candidate("finance-secret", "acl:finance")
    await store.add_candidates([*visible, hidden])

    audit = _audit()
    records = await _collect(store, _authz(), audit)

    assert {record.candidate.provenance.source_id for record in records} == {
        "public",
        "legal-a",
        "legal-b",
    }

    assert {record.stored_id for record in records} == {
        candidate_storage_id(item) for item in visible
    }

    assert len(audit) == 2
    assert audit.verify_chain()


async def test_viewless_approver_cannot_read_restricted_evidence() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-secret", "acl:finance")])
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    audit = _audit()

    assert await _collect(store, authz, audit) == []
    assert audit.verify_chain()


async def test_reviewer_role_is_not_sufficient() -> None:
    from ontos.authz import ONTOLOGY_REVIEWER

    store = NetworkxStore()
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_REVIEWER, "ontology")
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await _collect(store, authz, audit)

    assert audit.verify_chain()


async def test_limit_refuses_incomplete_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval, "MAX_APPROVAL_CANDIDATES", 1)

    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("legal-a", "acl:legal"),
            _candidate("legal-b", "acl:legal"),
        ]
    )
    audit = _audit()

    with pytest.raises(
        approval.ApprovalEvidenceLimitError,
        match="candidate limit",
    ):
        await _collect(store, _authz(), audit)

    assert audit.verify_chain()


@pytest.mark.parametrize("permission", ["approver", "view"])
async def test_revocation_between_page_and_final_check_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    permission: str,
) -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("legal", "acl:legal")])
    authz = _authz()
    audit = _audit()
    original = approval.inspect_candidates

    async def revoke_after_page(**kwargs: object):
        page = await original(**kwargs)
        if permission == "approver":
            authz.revoke("user:approver", ONTOLOGY_APPROVER, "ontology")
        else:
            authz.revoke("user:approver", VIEW, "acl:legal")
        return page

    monkeypatch.setattr(approval, "inspect_candidates", revoke_after_page)

    with pytest.raises(CandidateInspectionDeniedError):
        await _collect(store, authz, audit)

    assert audit.verify_chain()
