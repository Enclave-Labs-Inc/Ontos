"""M1: candidate inspection authorization, ACL isolation and Article-12 audit."""

from __future__ import annotations

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.runtime.candidate_inspection import (
    CANDIDATE_REFERENCE_KEY_LABEL,
    CandidateInspectionDeniedError,
    candidate_audit_reference,
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


def _assert_article12(
    audit: AuditEmitter, *, decision: str, subject: str, agent: str = "agent:test"
) -> None:
    """One complete record carrying the expected decision and identities.

    Field presence alone would pass even if a deny were logged as allow.
    """
    import json

    assert len(audit) == 1
    record = audit._chain[0]
    for field in ArticleTwelveField:
        assert field in record.article12, field.value
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "candidates"
    decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions == [{"policy": "ontology_candidate_inspection", "result": decision}]
    assert record.article12[ArticleTwelveField.ACTING_ON_BEHALF_OF] == subject
    assert record.article12[ArticleTwelveField.AGENT_IDENTITY] == agent
    if decision != "allow":
        assert record.article12[ArticleTwelveField.RESULT_FACT_IDS] == "[]"
    assert audit.verify_chain()


async def test_non_reviewer_denied_and_audited() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("acl:private")])

    authz = InMemoryAuthz()
    authz.grant("user:employee", VIEW, "acl:private")
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:employee",
            agent_identity="agent:test",
        )

    _assert_article12(audit, decision="deny", subject="user:employee")


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
    _assert_article12(populated_audit, decision="allow", subject="user:reviewer")
    _assert_article12(empty_audit, decision="allow", subject="user:reviewer")


async def test_reviewer_with_view_sees_only_authorized_candidates() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("acl:finance"),
            _candidate("acl:hr"),
        ]
    )

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
    _assert_article12(audit, decision="allow", subject="user:reviewer")


async def test_missing_authz_fails_closed_and_audits() -> None:
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=NetworkxStore(),
            authz=None,
            audit=audit,
            subject="user:reviewer",
            agent_identity="agent:test",
        )

    _assert_article12(audit, decision="deny", subject="user:reviewer")


async def test_public_candidate_still_requires_reviewer() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate(None)])
    authz = InMemoryAuthz()
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=audit,
            subject="user:employee",
            agent_identity="agent:test",
        )

    _assert_article12(audit, decision="deny", subject="user:employee")

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
    _assert_article12(reviewer_audit, decision="allow", subject="user:reviewer")


async def test_candidate_storage_failure_is_audited() -> None:
    class FailingStore:
        async def list_candidate_records(self, **kwargs):
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

    _assert_article12(audit, decision="storage_error", subject="user:reviewer")


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

    _assert_article12(audit, decision="authorization_error", subject="user:reviewer")


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

    _assert_article12(audit, decision="invalid_request", subject="user:reviewer")


async def test_missing_openfga_reviewer_relation_fails_closed() -> None:
    """An undefined production reviewer relation cannot expose candidates."""
    from ontos.authz.base import AuthzError
    from ontos.authz.openfga import OpenFGAAuthz

    class MissingRelationClient:
        async def check(self, *, body):
            assert body["relation"] == ONTOLOGY_REVIEWER
            raise RuntimeError("relation ontology_reviewer is undefined")

        async def list_objects(self, *, body):
            raise AssertionError("VIEW lookup must not occur")

    class GuardedStore:
        async def list_candidates(
            self,
            *,
            allowed_acls,
            include_public=False,
            limit=100,
        ):
            raise AssertionError("Candidate storage must not be accessed")

    authz = OpenFGAAuthz(
        client=MissingRelationClient(),
        store_id="test-store",
        authorization_model_id="model-without-reviewer",
    )
    audit = _audit()

    with pytest.raises(AuthzError):
        await inspect_candidates(
            candidate_store=GuardedStore(),
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="ontos-cli-local-operator",
            identity_assurance="operator_asserted",
        )

    _assert_article12(
        audit,
        decision="authorization_error",
        subject="user:reviewer",
        agent="ontos-cli-local-operator",
    )

    import json

    record = audit._chain[0]
    decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions[0]["result"] == "authorization_error"

    serialized = str(record.article12)
    assert "secret-person" not in serialized
    assert "restricted_relationship" not in serialized


async def test_candidate_audit_records_only_disclosed_ids() -> None:
    import json

    from ontos.storage.base import candidate_storage_id

    finance = _candidate("acl:finance")
    hr = _candidate("acl:hr")

    store = NetworkxStore()
    await store.add_candidates([finance, hr])

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    allowed_audit = _audit()
    returned, record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=allowed_audit,
        subject="user:reviewer",
        agent_identity="ontos-cli-local-operator",
        identity_assurance="operator_asserted",
    )

    assert returned == [finance]

    disclosed = json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
    key = allowed_audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
    assert disclosed == [candidate_audit_reference(key, candidate_storage_id(finance))]
    assert candidate_audit_reference(key, candidate_storage_id(hr)) not in disclosed
    # #72: never the raw, guessable candidate id.
    assert candidate_storage_id(finance) not in disclosed

    denied_audit = _audit()
    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=denied_audit,
            subject="user:employee",
            agent_identity="ontos-cli-local-operator",
            identity_assurance="operator_asserted",
        )

    denied_record = denied_audit._chain[0]

    assert json.loads(denied_record.article12[ArticleTwelveField.RESULT_FACT_IDS]) == []

    assert allowed_audit.verify_chain()
    assert denied_audit.verify_chain()


async def test_candidate_inspection_pagination_is_independently_audited() -> None:
    import json

    from ontos.storage.base import candidate_storage_id, encode_candidate_cursor

    original = _candidate("acl:finance")

    finance_candidates = [
        original.model_copy(
            update={
                "provenance": original.provenance.model_copy(
                    update={"source_id": f"finance-{i:03d}"}
                )
            }
        )
        for i in range(3)
    ]

    forbidden = _candidate("acl:hr")

    store = NetworkxStore()
    await store.add_candidates(list(reversed(finance_candidates)) + [forbidden])

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    audit = _audit()

    first_page, first_record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="ontos-cli-local-operator",
        identity_assurance="operator_asserted",
        limit=2,
    )

    assert first_page == finance_candidates[:2]

    cursor = encode_candidate_cursor(first_page[-1])

    second_page, second_record = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="ontos-cli-local-operator",
        identity_assurance="operator_asserted",
        limit=2,
        after=cursor,
    )

    assert second_page == finance_candidates[2:]
    assert set(map(candidate_storage_id, first_page)).isdisjoint(
        map(candidate_storage_id, second_page)
    )

    assert len(audit) == 2
    assert first_record != second_record

    stored_records = await store.list_candidate_records(
        allowed_acls=["acl:finance"],
        limit=100,
    )
    physical_ids = {item.candidate.provenance.source_id: item.stored_id for item in stored_records}

    for page, record in (
        (first_page, first_record),
        (second_page, second_record),
    ):
        actual_ids = json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
        key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
        expected_ids = [
            candidate_audit_reference(key, physical_ids[candidate.provenance.source_id])
            for candidate in page
        ]

        # Article-12 serialization may order IDs independently of the page.
        # Require exactly the disclosed physical IDs, without duplicates.
        assert len(actual_ids) == len(expected_ids)
        assert set(actual_ids) == set(expected_ids)

        decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        assert decisions[0]["result"] == "allow"

    # An arbitrary valid cursor must not bypass reviewer authorization.
    denied_audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await inspect_candidates(
            candidate_store=store,
            authz=authz,
            audit=denied_audit,
            subject="user:employee",
            agent_identity="ontos-cli-local-operator",
            identity_assurance="operator_asserted",
            limit=2,
            after=cursor,
        )

    assert len(denied_audit) == 1

    denied_record = denied_audit._chain[0]

    assert json.loads(denied_record.article12[ArticleTwelveField.RESULT_FACT_IDS]) == []

    decisions = json.loads(denied_record.article12[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions[0]["result"] == "deny"

    assert forbidden not in first_page + second_page
    assert audit.verify_chain()
    assert denied_audit.verify_chain()


async def test_unrelated_storage_value_error_is_audited_as_storage_error() -> None:
    import json

    class FailingStore:
        async def list_candidate_records(self, **kwargs):
            raise ValueError("internal storage validation failed")

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")

    audit = _audit()

    with pytest.raises(ValueError, match="internal storage validation failed"):
        await inspect_candidates(
            candidate_store=FailingStore(),
            authz=authz,
            audit=audit,
            subject="user:reviewer",
            agent_identity="ontos-cli-local-operator",
            identity_assurance="operator_asserted",
        )

    assert len(audit) == 1

    record = audit._chain[0]

    decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions[0]["result"] == "storage_error"

    disclosed_ids = json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS])
    assert disclosed_ids == []

    assert audit.verify_chain()


async def test_audit_log_cannot_confirm_guessed_candidates() -> None:
    """#72: an audit reader without the deployment key who correctly guesses a
    restricted triple must not be able to match it against the log, neither
    through disclosed candidate ids nor through cursor fingerprints."""
    import hashlib
    import json

    from ontos.storage.base import candidate_storage_id

    store = NetworkxStore()
    first = _candidate("acl:finance")
    second = first.model_copy(
        update={"provenance": first.provenance.model_copy(update={"source_id": "z-source"})}
    )
    await store.add_candidates([first, second])

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")
    audit = _audit()

    page_one = await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="agent",
        limit=1,
        paginated=True,
    )
    assert page_one.next_cursor is not None
    await inspect_candidates(
        candidate_store=store,
        authz=authz,
        audit=audit,
        subject="user:reviewer",
        agent_identity="agent",
        limit=1,
        after=page_one.next_cursor,
        paginated=True,
    )

    trail = json.dumps([dict(record.article12) for record in audit._chain])
    for guessed in (first, second):
        assert candidate_storage_id(guessed) not in trail
    assert hashlib.sha256(page_one.next_cursor.encode("utf-8")).hexdigest() not in trail

    # The key holder can still trace references back to candidates.
    key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
    assert candidate_audit_reference(key, candidate_storage_id(first)) in trail
    assert candidate_audit_reference(key, page_one.next_cursor) in trail

    # References are deployment-specific: another key yields other values.
    other_key = AuditEmitter(signing_key=b"another-deployment").derive_key(
        CANDIDATE_REFERENCE_KEY_LABEL
    )
    assert candidate_audit_reference(other_key, candidate_storage_id(first)) not in trail


def test_reference_key_is_separate_from_signing_key() -> None:
    audit = _audit()
    key = audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)
    assert key != b"candidate-inspection-test-key"
    assert key != audit.derive_key(b"some-other-purpose")
