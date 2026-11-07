"""M3-2: audited preflight must never imply approval or publication."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import ontos.runtime.ontology_approval as approval
from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.records import AddPatternChange
from ontos.ontology.approval_selection import ApprovalSuggestionNotFoundError
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore


def _ontology() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.1",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[
            RelationType(label="works_at"),
            RelationType(label="acquired"),
        ],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            )
        ],
    )


def _candidate(acl: str) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="person",
            subject_type="Person",
            subject_canonical_name="Person",
            predicate="acquired",
            object_id="company",
            object_type="Company",
            object_canonical_name="Company",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="test-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )


def _change() -> AddPatternChange:
    return AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )


def _audit() -> AuditEmitter:
    return AuditEmitter(b"m3-2-approval-preflight-test-key")


def _authz(*, reviewer_only: bool = False) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    relation = ONTOLOGY_REVIEWER if reviewer_only else ONTOLOGY_APPROVER
    authz.grant("user:operator", relation, "ontology")
    authz.grant("user:operator", VIEW, "acl:legal")
    return authz


def _decisions(audit: AuditEmitter) -> list[str]:
    return [
        json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])[0]["result"]
        for record in audit._chain
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_approve"
    ]


async def _run(
    store: NetworkxStore,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
    *,
    base_version: str = "0.1",
):
    return await approval._prepare_ontology_approval(
        candidate_store=store,
        authz=authz,
        audit=audit,
        ontology=_ontology(),
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        requested_change=_change(),
        acl_ref="acl:legal",
        base_version=base_version,
        new_version="0.2",
    )


async def test_successful_preflight_is_not_an_approval() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("acl:legal")])
    audit = _audit()

    result = await _run(store, _authz(), audit)

    assert result.change == _change()
    assert result.support_count == 1
    assert _decisions(audit) == [
        "request_started",
        "evidence_verified",
    ]
    assert audit.verify_chain()


async def test_hidden_and_nonexistent_evidence_have_same_outcome() -> None:
    decisions = []

    for evidence in ([], [_candidate("acl:finance")]):
        store = NetworkxStore()
        await store.add_candidates(evidence)
        audit = _audit()

        with pytest.raises(ApprovalSuggestionNotFoundError, match="No such suggestion"):
            await _run(store, _authz(), audit)

        decisions.append(_decisions(audit))
        assert audit.verify_chain()

    assert decisions == [
        ["request_started", "no_such_suggestion"],
        ["request_started", "no_such_suggestion"],
    ]


async def test_reviewer_without_approver_is_denied() -> None:
    store = NetworkxStore()
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await _run(store, _authz(reviewer_only=True), audit)

    assert _decisions(audit) == ["request_started", "deny"]
    assert audit.verify_chain()


async def test_stale_base_version_refused_before_candidate_read() -> None:
    store = NetworkxStore()
    store.list_candidate_records = AsyncMock(wraps=store.list_candidate_records)
    audit = _audit()

    with pytest.raises(ValueError, match="stale"):
        await _run(store, _authz(), audit, base_version="0.0")

    store.list_candidate_records.assert_not_awaited()
    assert _decisions(audit) == [
        "request_started",
        "invalid_request",
    ]
    assert audit.verify_chain()


async def test_revocation_after_collection_denies_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("acl:legal")])
    authz = _authz()
    audit = _audit()
    original = approval._collect_authorized_approval_candidates

    async def collect_then_revoke(**kwargs: object):
        records = await original(**kwargs)
        authz.revoke("user:operator", VIEW, "acl:legal")
        return records

    monkeypatch.setattr(
        approval,
        "_collect_authorized_approval_candidates",
        collect_then_revoke,
    )

    with pytest.raises(CandidateInspectionDeniedError):
        await _run(store, authz, audit)

    assert _decisions(audit) == ["request_started", "deny"]
    assert audit.verify_chain()


async def test_request_audit_failure_prevents_any_evidence_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    store.list_candidate_records = AsyncMock(wraps=store.list_candidate_records)
    audit = _audit()

    def fail_emit(**kwargs: object):
        raise OSError("audit unavailable")

    monkeypatch.setattr(audit, "emit", fail_emit)

    with pytest.raises(OSError, match="audit unavailable"):
        await _run(store, _authz(), audit)

    store.list_candidate_records.assert_not_awaited()
    assert len(audit) == 0
