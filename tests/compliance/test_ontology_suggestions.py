"""M2a compliance: authorized, audited ontology suggestions."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.ontology_suggestions import list_ontology_suggestions
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
            ),
            Pattern(
                subject_type="Company",
                predicate="acquired",
                object_type="Company",
            ),
        ],
    )


def _candidate(source: str, acl: str) -> RejectedCandidate:
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


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m2a-compliance-test-key")


def _reviewer(*acls: str) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    for acl in acls:
        authz.grant("user:reviewer", VIEW, acl)
    return authz


async def _suggest(
    store: NetworkxStore,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
):
    return await list_ontology_suggestions(
        candidate_store=store,
        authz=authz,
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
    )


def _request_decisions(audit: AuditEmitter) -> list[str]:
    decisions = []
    for record in audit._chain:
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions":
            policy = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
            decisions.extend(item["result"] for item in policy)
    return decisions


async def test_non_reviewer_denied_and_audited() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("secret", "acl:finance")])
    authz = InMemoryAuthz()
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == ["request_started", "deny"]
    assert audit.verify_chain()


async def test_viewless_matches_empty_store() -> None:
    populated = NetworkxStore()
    await populated.add_candidates([_candidate("hidden", "acl:finance")])
    empty = NetworkxStore()

    populated_audit = _audit()
    empty_audit = _audit()
    authz = _reviewer()

    populated_result, _ = await _suggest(populated, authz, populated_audit)
    empty_result, _ = await _suggest(empty, authz, empty_audit)

    assert populated_result == empty_result == []
    assert _request_decisions(populated_audit) == ["request_started", "allow"]
    assert populated_audit.verify_chain()


async def test_cross_acl_aggregation_is_forbidden() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-1", "acl:finance"),
            _candidate("finance-2", "acl:finance"),
            _candidate("hr-1", "acl:hr"),
        ]
    )
    authz = _reviewer("acl:finance", "acl:hr")
    audit = _audit()

    suggestions, _ = await _suggest(store, authz, audit)

    assert len(suggestions) == 2
    counts = {s.acl_ref: s.support_count for s in suggestions}
    assert counts == {"acl:finance": 2, "acl:hr": 1}
    assert audit.verify_chain()


async def test_pagination_counts_all_authorized_candidates() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate(f"doc-{i:03d}", "acl:finance") for i in range(125)])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1
    assert suggestions[0].support_count == 125
    assert len(suggestions[0].supporting_candidate_refs) == 125
    assert _request_decisions(audit) == ["request_started", "allow"]
    assert audit.verify_chain()


async def test_suggestions_do_not_enter_trusted_graph() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("untrusted", "acl:finance")])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1

    snapshot = await store.scan(allowed_acls=["acl:finance"])
    assert snapshot.facts == []
    assert snapshot.entities == []
    assert audit.verify_chain()


async def test_audit_does_not_contain_raw_candidate_ids() -> None:
    store = NetworkxStore()
    candidate = _candidate("secret-document", "acl:finance")
    await store.add_candidates([candidate])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1
    assert "secret-document" not in suggestions[0].model_dump_json()

    for record in audit._chain:
        assert "secret-document" not in str(record.article12)


async def test_storage_failure_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    audit = _audit()

    async def fail_storage(**kwargs: object) -> None:
        raise RuntimeError("sensitive storage error")

    monkeypatch.setattr(store, "list_candidate_records", fail_storage)

    with pytest.raises(RuntimeError, match="sensitive storage error"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "storage_error"]
    assert audit.verify_chain()
    assert "sensitive storage error" not in str(audit._chain[-1].article12)


async def test_candidate_limit_fails_without_partial_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.runtime.ontology_suggestions as service

    monkeypatch.setattr(service, "MAX_CANDIDATES", 1)

    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("first", "acl:finance"),
            _candidate("second", "acl:finance"),
        ]
    )
    audit = _audit()

    with pytest.raises(service.SuggestionLimitExceededError):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "incomplete_result"]
    assert audit.verify_chain()


async def test_generation_failure_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.runtime.ontology_suggestions as service

    store = NetworkxStore()
    await store.add_candidates([_candidate("first", "acl:finance")])
    audit = _audit()

    def fail_generation(**kwargs: object) -> None:
        raise RuntimeError("sensitive generation error")

    monkeypatch.setattr(service, "suggest_missing_patterns", fail_generation)

    with pytest.raises(RuntimeError, match="sensitive generation error"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "generation_error"]
    assert audit.verify_chain()
    assert "sensitive generation error" not in str(audit._chain[-1].article12)


async def test_audit_failure_prevents_candidate_storage_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    audit = _audit()
    storage_called = False

    async def record_storage_access(**kwargs: object) -> None:
        nonlocal storage_called
        storage_called = True
        raise AssertionError("Storage must not be read")

    def fail_audit(**kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "list_candidate_records", record_storage_access)
    monkeypatch.setattr(audit, "emit", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert not storage_called


async def test_view_revocation_during_retrieval_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence must not be disclosed after its VIEW permission is revoked."""
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-1", "acl:finance"),
            _candidate("finance-2", "acl:finance"),
        ]
    )

    authz = _reviewer("acl:finance")
    audit = _audit()

    original_list = store.list_candidate_records

    async def revoke_after_read(**kwargs: object):
        result = await original_list(**kwargs)
        authz.revoke("user:reviewer", VIEW, "acl:finance")
        return result

    monkeypatch.setattr(store, "list_candidate_records", revoke_after_read)

    with pytest.raises(PermissionError, match="Ontology suggestions denied"):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "authorization_error",
    ]
    assert audit.verify_chain()


async def test_authz_backend_failure_is_classified_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.authz.base import AuthzError

    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-secret", "acl:finance"),
        ]
    )

    authz = _reviewer("acl:finance")
    audit = _audit()

    async def fail_authorization(
        subject: str,
        relation: str,
        object_: str,
    ) -> bool:
        raise AuthzError("sensitive authorization backend failure")

    monkeypatch.setattr(authz, "check", fail_authorization)

    with pytest.raises(AuthzError):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "authorization_error",
    ]

    assert audit.verify_chain()

    for record in audit._chain:
        assert "finance-secret" not in str(record.article12)
        assert "sensitive authorization backend failure" not in str(record.article12)


async def test_public_candidates_are_separate_and_visible_to_reviewer() -> None:
    """Public candidates require reviewer authorization but no private VIEW grant."""
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("public-document", None),
            _candidate("private-document", "acl:finance"),
        ]
    )

    audit = _audit()
    suggestions, _ = await _suggest(store, _reviewer(), audit)

    assert len(suggestions) == 1
    assert suggestions[0].acl_ref is None
    assert suggestions[0].support_count == 1
    assert len(suggestions[0].supporting_candidate_refs) == 1
    assert _request_decisions(audit) == ["request_started", "allow"]
    assert audit.verify_chain()


async def test_final_allow_audit_failure_returns_no_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final request audit must succeed before results are returned."""
    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])
    audit = _audit()
    original_emit = audit.emit

    def fail_final_audit(**kwargs: object):
        if kwargs.get("tool_invoked") == "ontology_suggestions" and kwargs.get(
            "policy_decisions"
        ) == [{"policy": "ontology_suggestions", "result": "allow"}]:
            raise RuntimeError("final audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", fail_final_audit)

    with pytest.raises(RuntimeError, match="final audit unavailable"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == [
        "request_started",
        "audit_error",
    ]
    assert audit.verify_chain()


async def test_reviewer_role_revocation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking ontology_reviewer mid-request prevents disclosure."""
    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])

    authz = _reviewer("acl:finance")
    audit = _audit()
    original_list = store.list_candidate_records

    async def revoke_after_read(**kwargs: object):
        result = await original_list(**kwargs)
        authz.revoke("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
        return result

    monkeypatch.setattr(store, "list_candidate_records", revoke_after_read)

    with pytest.raises(PermissionError, match="Ontology suggestions denied"):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "authorization_error",
    ]
    assert audit.verify_chain()


async def test_page_audit_failure_is_not_classified_as_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-page inspection audit outage must be identified correctly."""
    from ontos.runtime.candidate_inspection import CandidateInspectionAuditError

    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])
    audit = _audit()
    original_emit = audit.emit

    def fail_inspection_audit(**kwargs: object):
        if kwargs.get("tool_invoked") == "candidates":
            raise RuntimeError("inspection audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", fail_inspection_audit)

    with pytest.raises(CandidateInspectionAuditError):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == [
        "request_started",
        "audit_error",
    ]
    assert audit.verify_chain()
