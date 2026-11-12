"""#124 compliance: affected-source discovery never leaks forbidden sources.

A restricted source the approver cannot VIEW must not appear, be
counted, or change the audit record compared to it not existing.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.approval_audit import sign_migration_record
from ontos.migration.records import AddPatternChange, MigrationRecord
from ontos.runtime.affected_sources import AffectedSources, discover_affected_sources
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

_KEY = b"affected-sources-compliance-key"


def _candidate(source_id: str, acl_ref: str | None) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="acme",
            subject_type="Company",
            subject_canonical_name="Acme",
            predicate="acquired",
            object_id="globex",
            object_type="Company",
            object_canonical_name="Globex",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id=source_id,
            extractor_id="test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence_score=0.9,
        ),
        acl_ref=acl_ref,
    )


def _record(audit: AuditEmitter) -> MigrationRecord:
    return sign_migration_record(
        record=MigrationRecord(
            schema_version=1,
            ontology_id="ontos.starter",
            from_version="0.1",
            to_version="0.2",
            migration_kind="identity",
            approval_query_id=UUID(int=1),
            approved_changes=[
                AddPatternChange(
                    action="add_pattern",
                    subject_type="Company",
                    predicate="acquired",
                    object_type="Company",
                )
            ],
        ),
        audit=audit,
    )


async def _run(*, include_forbidden: bool) -> tuple[AffectedSources, dict[str, object]]:
    audit = AuditEmitter(_KEY)
    store = NetworkxStore()
    candidates = [_candidate("public.md", None), _candidate("legal.md", "acl:legal")]
    if include_forbidden:
        candidates.append(_candidate("hr-investigation.md", "acl:hr"))
    await store.add_candidates(candidates)
    for candidate in candidates:
        await store.register_source(SourceRecord(candidate.provenance.source_id, candidate.acl_ref))

    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")
    authz.grant("user:approver", VIEW, "acl:legal")

    result = await discover_affected_sources(
        record=_record(audit),
        candidate_store=store,
        registry=store,
        authz=authz,
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
    )
    final = audit._chain[-1].article12
    assert final[ArticleTwelveField.TOOL_INVOKED] == "ontos.ontology.affected_sources"
    return result, json.loads(final[ArticleTwelveField.TOOL_ARGUMENTS])


@pytest.mark.asyncio
async def test_forbidden_source_is_indistinguishable_from_absent() -> None:
    with_forbidden, audit_with = await _run(include_forbidden=True)
    without_forbidden, audit_without = await _run(include_forbidden=False)

    assert with_forbidden == without_forbidden
    assert audit_with == audit_without
    listed = with_forbidden.registered + with_forbidden.unregistered
    assert all(source.acl_ref != "acl:hr" for source in listed)


@pytest.mark.asyncio
async def test_allow_record_has_article_twelve_fields() -> None:
    audit = AuditEmitter(_KEY)
    store = NetworkxStore()
    await store.add_candidates([_candidate("public.md", None)])
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")

    await discover_affected_sources(
        record=_record(audit),
        candidate_store=store,
        registry=store,
        authz=authz,
        audit=audit,
        subject="user:approver",
        agent_identity="ontos-cli-local-operator",
    )

    article12 = audit._chain[-1].article12
    for field in ArticleTwelveField:
        assert field in article12, field
    assert audit.verify_chain()
