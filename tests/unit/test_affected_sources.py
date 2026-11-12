"""#124: discover the sources an approved ontology change affects."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.approval_audit import sign_migration_record
from ontos.migration.records import (
    AddPatternChange,
    ApprovedChange,
    MapToExistingChange,
    MigrationRecord,
    NewRelationTypeChange,
)
from ontos.runtime.affected_sources import (
    AffectedSources,
    AffectedSourcesDeniedError,
    AffectedSourcesRequestError,
    discover_affected_sources,
)
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.scoped_reingest import _scope_reference
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord


def _candidate(
    *,
    source_id: str,
    acl_ref: str | None,
    predicate: str = "acquired",
    subject_type: str = "Company",
    reason: str = "pattern_not_in_ontology",
    ontology_id: str = "ontos.starter",
    ontology_version: str = "0.1",
) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id=f"{source_id}-s",
            subject_type=subject_type,
            subject_canonical_name="Acme",
            predicate=predicate,
            object_id=f"{source_id}-o",
            object_type="Company",
            object_canonical_name="Globex",
            llm_confidence=0.9,
        ),
        reason=reason,  # type: ignore[arg-type]
        provenance=CandidateProvenance(
            source_id=source_id,
            extractor_id="test",
            extractor_version="1",
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            confidence_score=0.9,
        ),
        acl_ref=acl_ref,
    )


def _audit() -> AuditEmitter:
    return AuditEmitter(b"affected-sources-test-key")


def _record(audit: AuditEmitter, change: ApprovedChange | None = None) -> MigrationRecord:
    record = MigrationRecord(
        schema_version=1,
        ontology_id="ontos.starter",
        from_version="0.1",
        to_version="0.2",
        migration_kind="identity",
        approval_query_id=uuid4(),
        approved_changes=[
            change
            or AddPatternChange(
                action="add_pattern",
                subject_type="Company",
                predicate="acquired",
                object_type="Company",
            )
        ],
    )
    return sign_migration_record(record=record, audit=audit)


def _authz(*acls: str) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:operator", ONTOLOGY_APPROVER, "ontology")
    for acl in acls:
        authz.grant("user:operator", VIEW, acl)
    return authz


async def _store(*candidates: RejectedCandidate, registered: tuple[str, ...] = ()) -> NetworkxStore:
    store = NetworkxStore()
    await store.add_candidates(list(candidates))
    for candidate in candidates:
        if candidate.provenance.source_id in registered:
            await store.register_source(
                SourceRecord(candidate.provenance.source_id, candidate.acl_ref)
            )
    return store


def _decisions(audit: AuditEmitter) -> list[tuple[str, str]]:
    out = []
    for record in audit._chain:
        tool = record.article12[ArticleTwelveField.TOOL_INVOKED]
        result = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])[0]["result"]
        out.append((tool, result))
    return out


async def _discover(
    store: NetworkxStore,
    audit: AuditEmitter,
    authz: InMemoryAuthz | None,
    record: MigrationRecord,
) -> AffectedSources:
    return await discover_affected_sources(
        record=record,
        candidate_store=store,
        registry=store,
        authz=authz,
        audit=audit,
        subject="user:operator",
        agent_identity="ontos-cli-local-operator",
    )


@pytest.mark.asyncio
async def test_splits_registered_and_unregistered_sources() -> None:
    audit = _audit()
    store = await _store(
        _candidate(source_id="b.md", acl_ref="acl:legal"),
        _candidate(source_id="a.md", acl_ref=None),
        _candidate(source_id="old.md", acl_ref="acl:legal"),
        registered=("a.md", "b.md"),
    )

    result = await _discover(store, audit, _authz("acl:legal"), _record(audit))

    assert result.registered == (
        SourceRecord("a.md", None),
        SourceRecord("b.md", "acl:legal"),
    )
    assert result.unregistered == (SourceRecord("old.md", "acl:legal"),)


@pytest.mark.asyncio
async def test_duplicate_evidence_from_one_source_is_listed_once() -> None:
    audit = _audit()
    first = _candidate(source_id="a.md", acl_ref=None)
    second = _candidate(source_id="a.md", acl_ref=None, subject_type="Company")
    second = second.model_copy(
        update={"raw_triple": second.raw_triple.model_copy(update={"subject_id": "other"})}
    )
    store = await _store(first, second, registered=("a.md",))

    result = await _discover(store, audit, _authz(), _record(audit))

    assert result.registered == (SourceRecord("a.md", None),)


@pytest.mark.asyncio
async def test_ignores_non_matching_evidence() -> None:
    audit = _audit()
    store = await _store(
        _candidate(source_id="pred.md", acl_ref=None, predicate="partnered_with"),
        _candidate(source_id="type.md", acl_ref=None, subject_type="Person"),
        _candidate(source_id="conf.md", acl_ref=None, reason="below_min_confidence"),
        _candidate(source_id="onto.md", acl_ref=None, ontology_id="other.ontology"),
        _candidate(source_id="older.md", acl_ref=None, ontology_version="0.0"),
        registered=("pred.md", "type.md", "conf.md", "onto.md", "older.md"),
    )

    result = await _discover(store, audit, _authz(), _record(audit))

    # Older versions of the same ontology still lack the approved relation.
    assert result.registered == (SourceRecord("older.md", None),)
    assert result.unregistered == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "predicate"),
    [
        (
            MapToExistingChange(
                action="map_to_existing",
                subject_type="Company",
                observed_predicate="bought",
                object_type="Company",
                maps_to="acquired",
            ),
            "bought",
        ),
        (
            NewRelationTypeChange(
                action="new_relation_type",
                subject_type="Company",
                relation_type="merged_into",
                object_type="Company",
            ),
            "merged_into",
        ),
    ],
)
async def test_matches_the_evidence_predicate_for_each_change_kind(
    change: ApprovedChange, predicate: str
) -> None:
    audit = _audit()
    store = await _store(
        _candidate(source_id="hit.md", acl_ref=None, predicate=predicate),
        _candidate(source_id="miss.md", acl_ref=None, predicate="acquired"),
        registered=("hit.md", "miss.md"),
    )

    result = await _discover(store, audit, _authz(), _record(audit, change))

    assert result.registered == (SourceRecord("hit.md", None),)


@pytest.mark.asyncio
async def test_unsigned_or_tampered_record_is_refused_and_audited() -> None:
    audit = _audit()
    store = await _store(_candidate(source_id="a.md", acl_ref=None), registered=("a.md",))
    tampered = _record(audit).model_copy(update={"to_version": "9.9"})

    with pytest.raises(AffectedSourcesRequestError):
        await _discover(store, audit, _authz(), tampered)

    assert _decisions(audit) == [("ontos.ontology.affected_sources", "invalid_request")]


@pytest.mark.asyncio
async def test_record_signed_with_another_key_is_refused() -> None:
    audit = _audit()
    store = await _store(_candidate(source_id="a.md", acl_ref=None), registered=("a.md",))
    foreign = _record(AuditEmitter(b"some-other-deployment"))

    with pytest.raises(AffectedSourcesRequestError):
        await _discover(store, audit, _authz(), foreign)


@pytest.mark.asyncio
async def test_non_approver_is_denied_and_audited() -> None:
    audit = _audit()
    store = await _store(_candidate(source_id="a.md", acl_ref=None), registered=("a.md",))
    authz = InMemoryAuthz()
    authz.grant("user:operator", VIEW, "acl:legal")

    with pytest.raises(AffectedSourcesDeniedError):
        await _discover(store, audit, authz, _record(audit))

    assert _decisions(audit)[-1] == ("ontos.ontology.affected_sources", "deny")


@pytest.mark.asyncio
async def test_missing_authz_backend_is_denied() -> None:
    audit = _audit()
    store = await _store(_candidate(source_id="a.md", acl_ref=None))

    with pytest.raises(AffectedSourcesDeniedError):
        await _discover(store, audit, None, _record(audit))

    assert _decisions(audit) == [("ontos.ontology.affected_sources", "deny")]


@pytest.mark.asyncio
async def test_allow_audit_carries_counts_and_reingest_scope_ref_only() -> None:
    audit = _audit()
    store = await _store(
        _candidate(source_id="secret-board-minutes.md", acl_ref="acl:legal"),
        registered=("secret-board-minutes.md",),
    )

    result = await _discover(store, audit, _authz("acl:legal"), _record(audit))

    final = audit._chain[-1]
    arguments = json.loads(final.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert arguments["affected_count"] == 1
    assert arguments["registered_count"] == 1
    assert arguments["scope_ref"] == _scope_reference(audit, result.registered)
    serialized = json.dumps(final.article12)
    assert "secret-board-minutes" not in serialized
    assert "acl:legal" not in serialized


@pytest.mark.asyncio
async def test_revocation_during_discovery_is_denied() -> None:
    audit = _audit()
    store = await _store(
        _candidate(source_id="a.md", acl_ref="acl:legal"),
        registered=("a.md",),
    )
    authz = _authz("acl:legal")

    original = store.get_source_record

    async def revoke_then_lookup(source_id: str, *, acl_ref: str | None) -> SourceRecord | None:
        authz.revoke("user:operator", VIEW, "acl:legal")
        return await original(source_id, acl_ref=acl_ref)

    store.get_source_record = revoke_then_lookup  # type: ignore[method-assign]

    with pytest.raises(AffectedSourcesDeniedError):
        await _discover(store, audit, authz, _record(audit))

    assert _decisions(audit)[-1] == ("ontos.ontology.affected_sources", "deny")
