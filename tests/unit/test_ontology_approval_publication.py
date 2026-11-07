"""M3-2: real Article-12 approvals and non-overwriting publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import ontos.runtime.ontology_approval as approval
from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.records import AddPatternChange
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
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


def _change() -> AddPatternChange:
    return AddPatternChange(
        action="add_pattern",
        subject_type="Person",
        predicate="acquired",
        object_type="Company",
    )


def _candidate() -> RejectedCandidate:
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
            source_id="source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence_score=0.9,
        ),
        acl_ref="acl:legal",
    )


def _authz() -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:operator", ONTOLOGY_APPROVER, "ontology")
    authz.grant("user:operator", VIEW, "acl:legal")
    return authz


def _audit() -> AuditEmitter:
    return AuditEmitter(b"m3-2-publication-test-key")


async def _store() -> NetworkxStore:
    store = NetworkxStore()
    await store.add_candidates([_candidate()])
    return store


async def _publish(
    tmp_path: Path,
    store: NetworkxStore,
    audit: AuditEmitter,
    *,
    authz: InMemoryAuthz | None = None,
):
    return await approval.approve_ontology_change(
        candidate_store=store,
        authz=authz if authz is not None else _authz(),
        audit=audit,
        ontology=_ontology(),
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
        requested_change=_change(),
        acl_ref="acl:legal",
        base_version="0.1",
        new_version="0.2",
        ontology_path=tmp_path / "ontology-0.2.yaml",
        migration_path=tmp_path / "migration-0.1-to-0.2.yaml",
    )


def _decisions(audit: AuditEmitter) -> list[str]:
    return [
        json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])[0]["result"]
        for record in audit._chain
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_approve"
    ]


async def test_success_persists_actual_approval_id(
    tmp_path: Path,
) -> None:
    audit = _audit()
    updated, record = await _publish(tmp_path, await _store(), audit)

    assert updated.version == "0.2"
    assert updated.is_valid_pattern("Person", "acquired", "Company")

    migration_file = tmp_path / "migration-0.1-to-0.2.yaml"
    ontology_file = tmp_path / "ontology-0.2.yaml"
    assert migration_file.is_file()
    assert ontology_file.is_file()

    migration_data = yaml.safe_load(migration_file.read_text())
    assert migration_data["approval_query_id"] == str(record.approval_query_id)
    assert record.approval_query_id.int != 0

    approval_event = audit.by_query_id(record.approval_query_id)
    assert approval_event is not None
    assert json.loads(approval_event.article12[ArticleTwelveField.POLICY_DECISIONS]) == [
        {"policy": "ontology_approval", "result": "approval_granted"}
    ]

    assert _decisions(audit) == [
        "request_started",
        "evidence_verified",
        "approval_granted",
        "published",
    ]
    assert audit.verify_chain()


async def test_existing_target_refuses_publication(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ontology-0.2.yaml"
    target.write_text("preexisting file")
    audit = _audit()

    with pytest.raises(FileExistsError):
        await _publish(tmp_path, await _store(), audit)

    assert target.read_text() == "preexisting file"
    assert not (tmp_path / "migration-0.1-to-0.2.yaml").exists()
    assert "approval_granted" not in _decisions(audit)
    assert _decisions(audit)[-1] == "invalid_request"
    assert audit.verify_chain()


async def test_grant_audit_failure_prevents_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()
    original_emit = audit.emit

    def refuse_grant(**kwargs: object):
        if kwargs.get("tool_invoked") == "ontology_approve":
            decisions = kwargs.get("policy_decisions")
            if decisions == [{"policy": "ontology_approval", "result": "approval_granted"}]:
                raise OSError("audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", refuse_grant)

    with pytest.raises(OSError, match="audit unavailable"):
        await _publish(tmp_path, await _store(), audit)

    assert list(tmp_path.glob("*.yaml")) == []
    assert "approval_granted" not in _decisions(audit)
    assert audit.verify_chain()


async def test_writer_failure_records_publication_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()

    def fail_writer(**kwargs: object):
        raise OSError("disk unavailable")

    monkeypatch.setattr(approval, "write_ontology_version", fail_writer)

    with pytest.raises(OSError, match="disk unavailable"):
        await _publish(tmp_path, await _store(), audit)

    assert list(tmp_path.glob("*.yaml")) == []
    assert _decisions(audit)[-2:] == [
        "approval_granted",
        "publication_failed",
    ]
    assert audit.verify_chain()


@pytest.mark.parametrize(
    "revoke_when",
    ["evidence_verified", "approval_granted"],
)
@pytest.mark.parametrize("permission", ["approver", "view"])
async def test_revocation_during_audit_prevents_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoke_when: str,
    permission: str,
) -> None:
    authz = _authz()
    audit = _audit()
    original_emit = audit.emit

    def revoke_during_audit(**kwargs: object):
        record = original_emit(**kwargs)

        if kwargs.get("tool_invoked") == "ontology_approve" and kwargs.get("policy_decisions") == [
            {
                "policy": "ontology_approval",
                "result": revoke_when,
            }
        ]:
            if permission == "approver":
                authz.revoke("user:operator", ONTOLOGY_APPROVER, "ontology")
            else:
                authz.revoke("user:operator", VIEW, "acl:legal")

        return record

    monkeypatch.setattr(audit, "emit", revoke_during_audit)

    with pytest.raises(
        approval.CandidateInspectionDeniedError,
        match="denied",
    ):
        await _publish(
            tmp_path,
            await _store(),
            audit,
            authz=authz,
        )

    decisions = _decisions(audit)

    if revoke_when == "evidence_verified":
        assert decisions[-1] == "deny"
        assert "approval_granted" not in decisions
    else:
        assert decisions[-2:] == [
            "approval_granted",
            "publication_failed",
        ]

    assert list(tmp_path.glob("*.yaml")) == []
    assert audit.verify_chain()


async def test_final_publication_audit_failure_rolls_back_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit()
    original_emit = audit.emit

    def refuse_published(**kwargs: object):
        if kwargs.get("tool_invoked") == "ontology_approve" and kwargs.get("policy_decisions") == [
            {
                "policy": "ontology_approval",
                "result": "published",
            }
        ]:
            raise OSError("final publication audit unavailable")

        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", refuse_published)

    with pytest.raises(
        OSError,
        match="final publication audit unavailable",
    ):
        await _publish(tmp_path, await _store(), audit)

    # Approval was recorded, but publication did not succeed.
    assert _decisions(audit)[-1] == "approval_granted"
    assert "published" not in _decisions(audit)

    # The files created by this operation must be rolled back.
    assert list(tmp_path.glob("*.yaml")) == []
    assert audit.verify_chain()
