"""M3-3: secure scoped re-ingestion execution without CLI exposure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.ingest.base import SourceDocument
from ontos.ingest.scoped_files import load_scoped_document
from ontos.ontology import load_ontology
from ontos.runtime.models import (
    ArticleTwelveField,
    Confidence,
    Entity,
    Provenance,
)
from ontos.runtime.scoped_execution import run_scoped_reingest
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    authorize_scoped_reingest,
)
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
SUBJECT = "user:operator"


class EmptyExtractor:
    id = "scoped-execution-test-extractor"

    def __init__(
        self,
        *,
        revoke: InMemoryAuthz | None = None,
    ) -> None:
        self.seen: list[str] = []
        self.revoke = revoke

    async def extract(
        self,
        input: ExtractionInput,
    ) -> ExtractionResult:
        self.seen.append(input.source_id)
        if self.revoke is not None:
            self.revoke.revoke(
                SUBJECT,
                VIEW,
                "acl:finance",
            )
        return ExtractionResult(facts=[], warnings=[])


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-3-execution-audit-test")


def _authz(*acls: str) -> InMemoryAuthz:
    backend = InMemoryAuthz()
    backend.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    for acl in acls:
        backend.grant(SUBJECT, VIEW, acl)
    return backend


def _decisions(audit: AuditEmitter) -> list[str]:
    results = []
    for record in audit._chain:
        parsed = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        results.append(parsed[0]["result"])
    return results


@pytest.mark.asyncio
async def test_executes_only_requested_document(
    tmp_path: Path,
) -> None:
    (tmp_path / "selected.md").write_text("Selected private content", encoding="utf-8")
    (tmp_path / "unrelated.md").write_text("Unrelated private content", encoding="utf-8")

    identity = SourceRecord("selected.md", "acl:finance")
    store = NetworkxStore()
    await store.register_source(identity)

    loaded: list[str] = []

    async def loader(record: SourceRecord):
        loaded.append(record.source_id)
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    extractor = EmptyExtractor()
    audit = _audit()

    report = await run_scoped_reingest(
        authz=_authz("acl:finance"),
        audit=audit,
        subject=SUBJECT,
        sources=[identity],
        registry=store,
        load_source=loader,
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
    )

    assert report.documents_seen == 1
    assert report.documents_extracted == 1
    assert loaded == ["selected.md"]
    assert extractor.seen == ["selected.md"]
    assert _decisions(audit)[-1] == "completed"
    assert audit.verify_chain()

    serialized = json.dumps([record.model_dump(mode="json") for record in audit._chain])
    assert "selected.md" not in serialized
    assert "Selected private content" not in serialized
    assert "acl:finance" not in serialized


@pytest.mark.asyncio
async def test_denied_operator_never_loads_or_extracts(
    tmp_path: Path,
) -> None:
    identity = SourceRecord("restricted.md", "acl:finance")
    store = NetworkxStore()
    await store.register_source(identity)

    loaded: list[str] = []

    async def loader(record: SourceRecord):
        loaded.append(record.source_id)
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    extractor = EmptyExtractor()
    audit = _audit()

    with pytest.raises(ScopedReingestDeniedError):
        await run_scoped_reingest(
            authz=_authz(),
            audit=audit,
            subject=SUBJECT,
            sources=[identity],
            registry=store,
            load_source=loader,
            extractor=extractor,
            resolver=None,
            store=store,
            ontology=load_ontology(STARTER),
        )

    assert loaded == []
    assert extractor.seen == []
    assert _decisions(audit) == ["deny"]


@pytest.mark.asyncio
async def test_revocation_during_extraction_prevents_writes(
    tmp_path: Path,
) -> None:
    (tmp_path / "restricted.md").write_text("Private data", encoding="utf-8")

    identity = SourceRecord("restricted.md", "acl:finance")
    store = NetworkxStore()
    await store.register_source(identity)

    backend = _authz("acl:finance")
    extractor = EmptyExtractor(revoke=backend)
    audit = _audit()

    async def loader(record: SourceRecord):
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    with pytest.raises(ScopedReingestDeniedError):
        await run_scoped_reingest(
            authz=backend,
            audit=audit,
            subject=SUBJECT,
            sources=[identity],
            registry=store,
            load_source=loader,
            extractor=extractor,
            resolver=None,
            store=store,
            ontology=load_ontology(STARTER),
        )

    assert extractor.seen == ["restricted.md"]
    assert _decisions(audit)[-1] == "failed"
    assert "deny" in _decisions(audit)
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_same_source_id_in_two_acl_scopes(
    tmp_path: Path,
) -> None:
    (tmp_path / "shared.md").write_text("Shared source text", encoding="utf-8")

    hr = SourceRecord("shared.md", "acl:hr")
    finance = SourceRecord("shared.md", "acl:finance")

    store = NetworkxStore()
    await store.register_source(hr)
    await store.register_source(finance)

    async def loader(record: SourceRecord):
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    extractor = EmptyExtractor()
    audit = _audit()

    report = await run_scoped_reingest(
        authz=_authz("acl:hr", "acl:finance"),
        audit=audit,
        subject=SUBJECT,
        sources=[hr, finance],
        registry=store,
        load_source=loader,
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
    )

    assert report.documents_seen == 2
    assert report.documents_extracted == 2
    assert extractor.seen == ["shared.md", "shared.md"]
    assert _decisions(audit)[-1] == "completed"
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_per_write_checks_do_not_repeat_allow_events(
    tmp_path: Path,
) -> None:
    """Multiple entity writes must not flood the audit chain."""
    (tmp_path / "selected.md").write_text(
        "Authorized test document",
        encoding="utf-8",
    )

    identity = SourceRecord("selected.md", "acl:finance")

    class TrackingStore(NetworkxStore):
        def __init__(self) -> None:
            super().__init__()
            self.upserts: list[str] = []

        async def upsert_entity(
            self,
            entity: Entity,
            *,
            acl_ref: str | None = None,
        ) -> None:
            self.upserts.append(entity.id)
            await super().upsert_entity(entity, acl_ref=acl_ref)

    class TwoEntitiesExtractor:
        id = "scoped-audit-noise-test"

        async def extract(
            self,
            input: ExtractionInput,
        ) -> ExtractionResult:
            provenance = Provenance(
                source_id=input.source_id,
                extractor_id=self.id,
                extractor_version="1",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            )
            return ExtractionResult(
                entities=[
                    Entity(
                        id="scoped-audit-person-one",
                        type="Person",
                        canonical_name="Alice",
                        provenance=provenance,
                    ),
                    Entity(
                        id="scoped-audit-person-two",
                        type="Person",
                        canonical_name="Bob",
                        provenance=provenance,
                    ),
                ],
                facts=[],
                warnings=[],
            )

    store = TrackingStore()
    await store.register_source(identity)

    async def loader(
        record: SourceRecord,
    ) -> SourceDocument:
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    audit = _audit()

    report = await run_scoped_reingest(
        authz=_authz("acl:finance"),
        audit=audit,
        subject=SUBJECT,
        sources=[identity],
        registry=store,
        load_source=loader,
        extractor=TwoEntitiesExtractor(),
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
    )

    assert report.documents_extracted == 1
    assert store.upserts == [
        "scoped-audit-person-one",
        "scoped-audit-person-two",
    ]

    decisions = _decisions(audit)

    # Preparation has two audited successful checks:
    # before loading and after preflight.
    # Registration and entity writes add no repeated allows.
    assert decisions.count("allow") == 2
    assert decisions[-1] == "completed"
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_suppressed_allow_still_audits_revocation() -> None:
    """Suppressing successful events must never suppress denials."""
    identity = SourceRecord("selected.md", "acl:finance")
    backend = _authz("acl:finance")
    audit = _audit()

    await authorize_scoped_reingest(
        authz=backend,
        audit=audit,
        subject=SUBJECT,
        sources=[identity],
        record_allow=False,
    )

    assert _decisions(audit) == []

    backend.revoke(SUBJECT, VIEW, "acl:finance")

    with pytest.raises(ScopedReingestDeniedError):
        await authorize_scoped_reingest(
            authz=backend,
            audit=audit,
            subject=SUBJECT,
            sources=[identity],
            record_allow=False,
        )

    assert _decisions(audit) == ["deny"]
    assert audit.verify_chain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_decision"),
    [
        ("mismatch", "deny"),
        ("missing", "deny"),
        ("lookup_error", "scope_unavailable"),
    ],
)
async def test_write_gate_distinguishes_registry_mismatch_from_lookup_failure(
    tmp_path: Path,
    failure_mode: str,
    expected_decision: str,
) -> None:
    """After preflight, a changed scope is not a registry lookup failure."""
    (tmp_path / "selected.md").write_text(
        "Private test content",
        encoding="utf-8",
    )
    identity = SourceRecord("selected.md", "acl:finance")

    class ChangingRegistryStore(NetworkxStore):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def get_source_record(
            self,
            source_id: str,
            *,
            acl_ref: str | None,
        ) -> SourceRecord | None:
            self.lookups += 1

            # The first two lookups happen during preparation.
            # Simulate a registry change/failure at the write gate.
            if self.lookups >= 3:
                if failure_mode == "mismatch":
                    return SourceRecord(source_id, "acl:other")
                if failure_mode == "missing":
                    return None
                raise RuntimeError("private registry failure detail")

            return await super().get_source_record(
                source_id,
                acl_ref=acl_ref,
            )

    store = ChangingRegistryStore()
    await store.register_source(identity)

    async def loader(record: SourceRecord):
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    audit = _audit()
    extractor = EmptyExtractor()

    with pytest.raises(
        ScopedReingestDeniedError,
        match="Scoped re-ingestion denied",
    ):
        await run_scoped_reingest(
            authz=_authz("acl:finance"),
            audit=audit,
            subject=SUBJECT,
            sources=[identity],
            registry=store,
            load_source=loader,
            extractor=extractor,
            resolver=None,
            store=store,
            ontology=load_ontology(STARTER),
        )

    assert store.lookups == 3
    assert extractor.seen == ["selected.md"]

    decisions = _decisions(audit)
    assert decisions == [
        "allow",
        "allow",
        expected_decision,
        "failed",
    ]
    assert "completed" not in decisions
    assert audit.verify_chain()

    serialized = json.dumps([record.model_dump(mode="json") for record in audit._chain])
    assert "private registry failure detail" not in serialized
    assert "acl:finance" not in serialized
    assert "acl:other" not in serialized
