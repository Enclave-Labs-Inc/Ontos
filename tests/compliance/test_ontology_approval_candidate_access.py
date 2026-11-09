"""M3-2: approval candidate reads against real ACL-filtered storage."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.runtime.candidate_inspection import (
    CandidateInspectionDeniedError,
    inspect_candidates,
)
from ontos.runtime.models import ArticleTwelveField
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
            extractor_id="scripted-test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )


def _authz(*acls: str) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    for acl in acls:
        authz.grant("user:approver", VIEW, acl)
    return authz


def _audit() -> AuditEmitter:
    return AuditEmitter(b"m3-2-real-store-test-key")


async def _inspect(
    store: NetworkxStore,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
) -> list[RejectedCandidate]:
    candidates, _ = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
        identity_assurance="operator_asserted",
        required_relation=ONTOLOGY_APPROVER,
    )
    return candidates


def _decisions(audit: AuditEmitter) -> list[str]:
    return [
        json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])[0]["result"]
        for record in audit._chain
    ]


async def test_real_store_excludes_hidden_evidence() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("public", None),
            _candidate("legal", "acl:legal"),
            _candidate("finance-secret", "acl:finance"),
        ]
    )
    audit = _audit()

    visible = await _inspect(store, _authz("acl:legal"), audit)

    assert {item.provenance.source_id for item in visible} == {
        "public",
        "legal",
    }
    assert _decisions(audit) == ["allow"]
    assert audit.verify_chain()

    logged = audit._chain[0].article12[ArticleTwelveField.RESULT_FACT_IDS]
    assert len(json.loads(logged)) == 2
    assert "finance-secret" not in logged
    assert "person-finance-secret" not in logged


async def test_hidden_only_matches_empty_store() -> None:
    populated = NetworkxStore()
    await populated.add_candidates([_candidate("finance-secret", "acl:finance")])
    empty = NetworkxStore()
    populated_audit = _audit()
    empty_audit = _audit()
    authz = _authz()

    populated_result = await _inspect(populated, authz, populated_audit)
    empty_result = await _inspect(empty, authz, empty_audit)

    assert populated_result == empty_result == []
    assert _decisions(populated_audit) == _decisions(empty_audit) == ["allow"]
    for audit in (populated_audit, empty_audit):
        assert json.loads(audit._chain[0].article12[ArticleTwelveField.RESULT_FACT_IDS]) == []
        assert audit.verify_chain()


@pytest.mark.parametrize("revoked_permission", ["approver", "view"])
async def test_revocation_during_real_store_read_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    revoked_permission: str,
) -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("legal", "acl:legal")])
    authz = _authz("acl:legal")
    audit = _audit()
    original_read = store.list_candidate_records

    async def read_then_revoke(
        *,
        allowed_acls: list[str],
        include_public: bool = False,
        limit: int = 100,
        after: str | None = None,
    ):
        records = await original_read(
            allowed_acls=allowed_acls,
            include_public=include_public,
            limit=limit,
            after=after,
        )
        assert len(records) == 1

        if revoked_permission == "approver":
            authz.revoke("user:approver", ONTOLOGY_APPROVER, "ontology")
        else:
            authz.revoke("user:approver", VIEW, "acl:legal")

        return records

    monkeypatch.setattr(store, "list_candidate_records", read_then_revoke)

    with pytest.raises(CandidateInspectionDeniedError):
        await _inspect(store, authz, audit)

    assert _decisions(audit) == ["deny"]
    assert json.loads(audit._chain[0].article12[ArticleTwelveField.RESULT_FACT_IDS]) == []
    assert audit.verify_chain()
