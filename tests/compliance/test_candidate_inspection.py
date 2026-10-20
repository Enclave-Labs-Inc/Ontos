"""M1: candidate inspection authorization, ACL isolation and Article-12 audit."""

from __future__ import annotations

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDenied,
    inspect_candidates,
)
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore


def _candidate(acl_ref: str | None) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="secret-person",
            subject_type="Person",
            subject_canonical_name="Secret Person",
            predicate="restricted_relationship",
            object_id="secret-company",
            object_type="Company",
            object_canonical_name="Secret Company",
            llm_confidence=0.8,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="private-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.8,
        ),
        acl_ref=acl_ref,
    )


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"candidate-inspection-test-key")


def _assert_article12(audit: AuditEmitter) -> None:
    assert len(audit) == 1
    record = audit._chain[0]
    for field in ArticleTwelveField:
        assert field in record.article12, field.value
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "candidates"
    assert audit.verify_chain()


async def test_non_reviewer_denied_and_audited() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("acl:private")])

    authz = InMemoryAuthz()
    authz.grant("user:employee", VIEW, "acl:private")
    audit = _audit()

    with pytest.raises(CandidateInspectionDenied):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:employee",
            agent_identity="agent:test",
        )

    _assert_article12(audit)


async def test_reviewer_without_view_sees_same_empty_result_as_empty_store() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")

    populated = NetworkxStore()
    await populated.add_candidates([_candidate("acl:private")])
    empty = NetworkxStore()

    populated_audit = _audit()
    empty_audit = _audit()

    populated_result, _ = await inspect_candidates(
        candidate_store=populated,
        authz=authz,
        audit=populated_audit,
        subject="user:reviewer",
        agent_identity="agent:test",
    )
    empty_result, _ = await inspect_candidates(
        candidate_store=empty,
        authz=authz,
        audit=empty_audit,
        subject="user:reviewer",
        agent_identity="agent:test",
    )

    assert populated_result == empty_result == []
    _assert_article12(populated_audit)
    _assert_article12(empty_audit)


async def test_reviewer_with_view_sees_only_authorized_candidates() -> None:
    store = NetworkxStore()
    await store.add_candidates([
        _candidate("acl:finance"),
        _candidate("acl:hr"),
    ])

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")
    audit = _audit()

    candidates, record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="agent:test",
    )

    assert len(candidates) == 1
    assert candidates[0].acl_ref == "acl:finance"
    assert audit.by_query_id(record.query_id) == record
    _assert_article12(audit)


async def test_missing_authz_fails_closed_and_audits() -> None:
    audit = _audit()

    with pytest.raises(CandidateInspectionDenied):
        await inspect_candidates(
            candidate_store=NetworkxStore(),
            authz=None,
            audit=audit,
            subject="user:reviewer",
            agent_identity="agent:test",
        )

    _assert_article12(audit)


async def test_public_candidate_still_requires_reviewer() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate(None)])
    authz = InMemoryAuthz()
    audit = _audit()

    with pytest.raises(CandidateInspectionDenied):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:employee",
            agent_identity="agent:test",
        )

    _assert_article12(audit)

    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    reviewer_audit = _audit()
    candidates, _ = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=reviewer_audit,
        subject="user:reviewer",
        agent_identity="agent:test",
    )
    assert len(candidates) == 1
    assert candidates[0].acl_ref is None
    _assert_article12(reviewer_audit)


async def test_candidate_storage_failure_is_audited() -> None:
    class FailingStore:
        async def list_candidates(self, **kwargs):
            raise RuntimeError("storage unavailable")

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    audit = _audit()

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await inspect_candidates(
            candidate_store=FailingStore(),
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="agent:test",
        )

    _assert_article12(audit)


async def test_authorization_failure_is_audited() -> None:
    class FailingAuthz(InMemoryAuthz):
        async def check(self, subject, relation, object_):
            raise RuntimeError("authz unavailable")

    audit = _audit()
    with pytest.raises(RuntimeError, match="authz unavailable"):
        await inspect_candidates(
            candidate_store=NetworkxStore(),
            authz=FailingAuthz(),
            audit=audit,
            subject="user:reviewer",
            agent_identity="agent:test",
        )

    _assert_article12(audit)



@pytest.mark.parametrize("limit", [0, -1, 101, True])
async def test_invalid_inspection_limit_is_audited(limit) -> None:
    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    audit = _audit()

    with pytest.raises(ValueError, match="limit"):
        await inspect_candidates(
            candidate_store=NetworkxStore(),
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="agent:test",
            limit=limit,
        )

    _assert_article12(audit)
