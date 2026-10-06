"""#48 compliance: cross-ACL entity attribute writes are rejected atomically.

Three invariants:

1. End-to-end rejection through the pipeline (both FAIL_FAST and
   SKIP_AND_LOG dispatch).
2. #46's `merges_for_entity` visibility stays coherent after the fix —
   the "stored ACL but different-scope attrs" state cannot occur.
3. The rejection fires a structured audit event compliance operators
   can grep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from ontos.extraction.base import ExtractionResult
from ontos.ingest import SourceDocument
from ontos.ontology import load_ontology
from ontos.pipeline import ErrorPolicy, IngestPipeline
from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Entity, Provenance
from ontos.storage.base import CrossAclUpsertError
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _prov() -> Provenance:
    return Provenance(
        source_id="t",
        extractor_id="t",
        extractor_version="0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


def _entity(canonical_name: str, properties: dict[str, str] | None = None) -> Entity:
    return Entity(
        id="person:alice",
        type="Person",
        canonical_name=canonical_name,
        properties=properties or {},
        provenance=_prov(),
    )


class _TwoDocConnector:
    """Finance doc first, HR doc second — the exact #48 scenario."""

    id = "two-doc-conn"
    source_kind = "test"

    async def iter_documents(self):
        yield SourceDocument(source_id="finance", text="x", acl_ref="acl:finance")
        yield SourceDocument(source_id="hr", text="x", acl_ref="acl:hr")


class _FixtureExtractor:
    id = "test-extractor"
    version = "0"

    def __init__(self, script: dict[str, list[Entity]]) -> None:
        self._script = script

    async def extract(self, inp) -> ExtractionResult:
        return ExtractionResult(entities=self._script[inp.source_id], facts=[])


async def test_cross_acl_entity_merge_rejected_through_pipeline_fail_fast() -> None:
    """FAIL_FAST: cross-ACL conflict propagates CrossAclUpsertError
    out of IngestPipeline.run()."""
    ontology = load_ontology(STARTER)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=_TwoDocConnector(),
        extractor=_FixtureExtractor(
            {
                "finance": [_entity("Alice Finance", {"dept": "finance"})],
                "hr": [_entity("Alice HR", {"dept": "hr"})],
            }
        ),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.FAIL_FAST,
    )
    with pytest.raises(CrossAclUpsertError) as exc:
        await pipeline.run()
    assert exc.value.entity_id == "person:alice"
    assert exc.value.stored_acl == "acl:finance"
    assert exc.value.attempted_acl == "acl:hr"

    # Finance attrs survived; no HR attrs leaked under the finance stamp.
    attrs = store._graph.nodes["person:alice"]  # noqa: SLF001
    assert attrs["canonical_name"] == "Alice Finance"
    assert attrs["properties"] == {"dept": "finance"}


async def test_cross_acl_entity_merge_rejected_through_pipeline_skip_and_log() -> None:
    """SKIP_AND_LOG: HR doc lands in IngestReport.errors; finance attrs
    survive untouched; the whole HR doc is skipped (no partial write)."""
    ontology = load_ontology(STARTER)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=_TwoDocConnector(),
        extractor=_FixtureExtractor(
            {
                "finance": [_entity("Alice Finance", {"dept": "finance"})],
                "hr": [_entity("Alice HR", {"dept": "hr"})],
            }
        ),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    )
    report = await pipeline.run()

    assert report.documents_seen == 2
    assert len(report.errors) == 1
    assert report.errors[0].source_id == "hr"
    assert "cross-ACL" in report.errors[0].reason

    attrs = store._graph.nodes["person:alice"]  # noqa: SLF001
    assert attrs["acl_ref"] == "acl:finance"
    assert attrs["canonical_name"] == "Alice Finance"
    assert attrs["properties"] == {"dept": "finance"}


async def test_merges_for_entity_visibility_coherent_after_cross_acl_fix() -> None:
    """#46's _merge_record_visible pre-filter inherited the #48 bug —
    a Finance-stamped node whose attrs had been silently rewritten under
    HR would still show the merge edge to a Finance caller consistently
    with wrong attrs. Post-#48: the HR write never lands, so the stamp +
    attrs stay coherent, and the merge visibility check is trustworthy.
    """
    store = NetworkxStore()
    # Finance-stamp Alice first.
    await store.upsert_entity(
        _entity("Alice Finance", {"dept": "finance"}),
        acl_ref="acl:finance",
    )
    # Separately upsert a merged candidate under finance (same scope).
    await store.upsert_entity(
        Entity(
            id="person:alice-alt",
            type="Person",
            canonical_name="A. Finance",
            provenance=_prov(),
        ),
        acl_ref="acl:finance",
    )
    # Record the merge.
    await store.record_merge(
        MergeRecord(
            canonical_id="person:alice",
            merged_ids=["person:alice", "person:alice-alt"],
            resolver_id="r",
            resolver_version="0",
            resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    # Cross-ACL attempt from HR — rejected, so no silent attr overwrite.
    with pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(
            _entity("Alice HR", {"dept": "hr"}),
            acl_ref="acl:hr",
        )

    # Finance caller still sees the merge; attrs remain Finance's.
    finance_visible = await store.merges_for_entity("person:alice", allowed_acls=["acl:finance"])
    assert len(finance_visible) == 1
    attrs = store._graph.nodes["person:alice"]  # noqa: SLF001
    assert attrs["canonical_name"] == "Alice Finance"
    # HR-only caller cannot see this merge (endpoints both finance).
    hr_visible = await store.merges_for_entity("person:alice", allowed_acls=["acl:hr"])
    assert hr_visible == []


async def test_cross_acl_rejection_fires_structured_audit_event() -> None:
    """Operator-visible audit trail — a structlog `cross-acl-upsert-rejected`
    event lets compliance operators grep for attempted boundary crossings
    without parsing exception stack traces."""
    store = NetworkxStore()
    await store.upsert_entity(_entity("Alice Finance"), acl_ref="acl:finance")
    with structlog.testing.capture_logs() as logs, pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(_entity("Alice HR"), acl_ref="acl:hr")
    rejected = [log for log in logs if log.get("event") == "cross-acl-upsert-rejected"]
    assert len(rejected) == 1
    assert rejected[0]["entity_id"] == "person:alice"
    assert rejected[0]["stored_acl"] == "acl:finance"
    assert rejected[0]["attempted_acl"] == "acl:hr"


async def test_audit_log_does_not_include_attempted_attribute_content() -> None:
    """PR #49 review fix: the audit log event must NOT include
    `attempted_canonical_name` (or any other attempted attribute
    content). Log sinks are typically readable by operators without
    the restricted ACL — copying attribute content there copies
    restricted content into a less-protected place."""
    store = NetworkxStore()
    await store.upsert_entity(_entity("Alice Finance"), acl_ref="acl:finance")
    with structlog.testing.capture_logs() as logs, pytest.raises(CrossAclUpsertError):
        await store.upsert_entity(
            _entity("Alice HR SECRET DATA", {"ssn": "123"}),
            acl_ref="acl:hr",
        )
    rejected = [log for log in logs if log.get("event") == "cross-acl-upsert-rejected"]
    assert len(rejected) == 1
    assert "attempted_canonical_name" not in rejected[0]
    # And none of the restricted attempted-attr values leak via any
    # other key.
    event_str = str(rejected[0])
    assert "Alice HR SECRET DATA" not in event_str
    assert "123" not in event_str


async def test_ingest_report_errors_do_not_leak_entity_id_or_stored_acl() -> None:
    """PR #49 review fix (1a): `IngestReport.errors[].reason` must NOT
    carry the stored ACL label or the entity id — a public caller
    inspecting the report would otherwise learn that a restricted
    entity exists and which ACL guards it. CLAUDE.md: never leak the
    existence of a forbidden node. The detailed stored_acl +
    attempted_acl pair lives in the audit log instead."""
    ontology = load_ontology(STARTER)
    store = NetworkxStore()
    pipeline = IngestPipeline(
        connector=_TwoDocConnector(),
        extractor=_FixtureExtractor(
            {
                "finance": [_entity("Alice Finance", {"dept": "finance"})],
                "hr": [_entity("Alice HR", {"dept": "hr"})],
            }
        ),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    )
    report = await pipeline.run()

    assert len(report.errors) == 1
    reason = report.errors[0].reason
    # Generic text only — no restricted ACL label, no entity id.
    assert reason == "cross-ACL entity conflict; document skipped"
    assert "acl:finance" not in reason
    assert "person:alice" not in reason


async def test_cross_acl_pre_check_leaves_store_untouched_whole_doc_skip() -> None:
    """PR #49 review fix (1b): if ANY entity in a doc would conflict,
    the WHOLE doc is skipped and the store is left untouched. Pre-review
    the pipeline wrote entities one-by-one — a conflict partway through
    left earlier entities stamped with this doc's ACL while the facts
    never landed. The pre-check pass makes "whole-doc atomic" true."""
    ontology = load_ontology(STARTER)
    store = NetworkxStore()
    # Pre-populate "person:alice" with Finance ACL (will conflict).
    await store.upsert_entity(
        _entity("Alice Finance", {"dept": "finance"}),
        acl_ref="acl:finance",
    )

    # Second doc from HR ingests TWO new-looking entities (person:bob,
    # person:alice). person:bob has no stored ACL yet so a naive
    # one-by-one loop would stamp it with acl:hr before hitting the
    # alice conflict. The pre-check must prevent that write entirely.
    bob = Entity(
        id="person:bob",
        type="Person",
        canonical_name="Bob HR",
        provenance=_prov(),
    )
    alice_hr = _entity("Alice HR", {"dept": "hr"})

    class _HrConnector:
        id = "hr-conn"
        source_kind = "test"

        async def iter_documents(self):
            yield SourceDocument(source_id="hr", text="x", acl_ref="acl:hr")

    pipeline = IngestPipeline(
        connector=_HrConnector(),
        extractor=_FixtureExtractor({"hr": [bob, alice_hr]}),
        resolver=None,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    )
    report = await pipeline.run()

    assert len(report.errors) == 1
    assert report.errors[0].source_id == "hr"
    # person:bob must NOT exist in the store — pre-check caught the
    # conflict before any write landed.
    assert "person:bob" not in store._graph.nodes  # noqa: SLF001
    # person:alice unchanged — Finance's attrs survive.
    attrs = store._graph.nodes["person:alice"]  # noqa: SLF001
    assert attrs["acl_ref"] == "acl:finance"
    assert attrs["canonical_name"] == "Alice Finance"


async def test_entity_acl_read_lookup_returns_stored_or_none() -> None:
    """PR #49: new `GraphStore.entity_acl` read seam powers the
    pipeline pre-check. Returns the stored acl_ref when the entity
    exists with one, None when it doesn't exist OR is public."""
    store = NetworkxStore()
    assert await store.entity_acl("unknown") is None
    await store.upsert_entity(_entity("Alice Public"))  # acl_ref=None
    assert await store.entity_acl("person:alice") is None
    await store.upsert_entity(_entity("Bob Finance"), acl_ref="acl:finance")  # new id override
    # Add a separately-ACL'd entity via explicit upsert to avoid the
    # None → X upgrade path on person:alice.
    bob = Entity(
        id="person:bob",
        type="Person",
        canonical_name="Bob",
        provenance=_prov(),
    )
    await store.upsert_entity(bob, acl_ref="acl:finance")
    assert await store.entity_acl("person:bob") == "acl:finance"
