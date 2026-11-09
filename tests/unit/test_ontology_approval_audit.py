"""M3-2: Article-12 approval audit records and privacy guarantees."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.migration.records import MapToExistingChange
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.ontology_approval import record_ontology_approval_event


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-2-approval-audit-test-key")


def _change() -> MapToExistingChange:
    return MapToExistingChange(
        action="map_to_existing",
        subject_type="Person",
        observed_predicate="sensitive_employment_relation",
        object_type="Company",
        maps_to="works_at",
    )


async def _emit(
    audit: AuditEmitter,
    decision: str,
    **extra: object,
):
    from typing import cast

    from ontos.runtime.ontology_approval import ApprovalAuditDecision

    return await record_ontology_approval_event(
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
        ontology_id="ontos.starter",
        base_version="0.1",
        new_version="0.2",
        requested_change=_change(),
        acl_ref="acl:sensitive-legal-documents",
        decision=cast(ApprovalAuditDecision, decision),
        **extra,
    )


async def test_granted_approval_has_real_retrievable_query_id() -> None:
    audit = _audit()
    opaque_ref = "a" * 64

    record = await _emit(
        audit,
        "approval_granted",
        support_count=1,
        supporting_candidate_refs=[opaque_ref],
    )

    assert audit.by_query_id(record.query_id) == record
    assert len(audit) == 1
    assert audit.verify_chain()

    for field in ArticleTwelveField:
        assert field in record.article12

    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_approve"

    decisions = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
    assert decisions == [{"policy": "ontology_approval", "result": "approval_granted"}]

    assert json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS]) == [opaque_ref]

    args = json.loads(record.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert args["identity_assurance"] == "operator_asserted"
    assert args["support_count"] == 1
    assert len(args["change_ref"]) == 64
    assert len(args["scope_ref"]) == 64

    serialized = record.model_dump_json()
    assert "sensitive_employment_relation" not in serialized
    assert "acl:sensitive-legal-documents" not in serialized


@pytest.mark.parametrize(
    "decision",
    [
        "request_started",
        "deny",
        "no_such_suggestion",
        "invalid_request",
        "authorization_error",
        "storage_error",
        "publication_failed",
        "failure",
    ],
)
async def test_non_granted_decisions_do_not_disclose_evidence(
    decision: str,
) -> None:
    audit = _audit()

    record = await _emit(
        audit,
        decision,
        supporting_candidate_refs=["b" * 64],
        support_count=10,
    )

    assert json.loads(record.article12[ArticleTwelveField.RESULT_FACT_IDS]) == []

    args = json.loads(record.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert args["support_count"] == 0
    assert audit.verify_chain()


async def test_publication_record_is_distinct_from_approval() -> None:
    audit = _audit()
    granted = await _emit(
        audit,
        "approval_granted",
        support_count=1,
        supporting_candidate_refs=["c" * 64],
    )
    published = await _emit(
        audit,
        "published",
        support_count=1,
        supporting_candidate_refs=["c" * 64],
    )

    assert granted.query_id != published.query_id
    assert audit.by_query_id(granted.query_id) == granted
    assert audit.by_query_id(published.query_id) == published
    assert audit.verify_chain()


async def test_audit_failure_is_not_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()

    def refuse(**kwargs: object):
        raise OSError("audit unavailable")

    monkeypatch.setattr(audit, "emit", refuse)

    with pytest.raises(OSError, match="audit unavailable"):
        await _emit(audit, "approval_granted")

    assert len(audit) == 0


async def test_raw_candidate_reference_is_rejected() -> None:
    audit = _audit()

    with pytest.raises(ValueError, match="opaque candidate references"):
        await _emit(
            audit,
            "approval_granted",
            supporting_candidate_refs=["raw-private-candidate-id"],
        )

    assert len(audit) == 0
