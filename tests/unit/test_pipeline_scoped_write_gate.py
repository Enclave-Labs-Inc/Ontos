"""M3-3: authorization must be rechecked before ingestion writes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.ingest import TextConnector
from ontos.ontology import load_ontology
from ontos.pipeline.ingest import ErrorPolicy, IngestPipeline
from ontos.runtime.models import Confidence, Entity, Provenance
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    authorize_scoped_reingest,
)
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"

SUBJECT = "user:operator"
SOURCE = SourceRecord("doc.txt", "acl:finance")


class EmptyExtractor:
    id = "scoped-write-gate-test"

    def __init__(
        self,
        on_extract: Callable[[], None] | None = None,
    ) -> None:
        self.on_extract = on_extract

    async def extract(
        self,
        input: ExtractionInput,
    ) -> ExtractionResult:
        if self.on_extract is not None:
            self.on_extract()
        return ExtractionResult(facts=[], warnings=[])


class RecordingStore(NetworkxStore):
    def __init__(self) -> None:
        super().__init__()
        self.registration_attempts: list[SourceRecord] = []

    async def register_source(self, record: SourceRecord) -> None:
        self.registration_attempts.append(record)
        await super().register_source(record)


def _authz() -> InMemoryAuthz:
    backend = InMemoryAuthz()
    backend.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    backend.grant(SUBJECT, VIEW, "acl:finance")
    return backend


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m3-3-write-gate-test-key")


def _pipeline(
    *,
    store: RecordingStore,
    extractor: EmptyExtractor,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
    error_policy: ErrorPolicy = ErrorPolicy.FAIL_FAST,
) -> IngestPipeline:
    async def before_write(identity: SourceRecord) -> None:
        await authorize_scoped_reingest(
            authz=authz,
            audit=audit,
            subject=SUBJECT,
            sources=[identity],
        )

    return IngestPipeline(
        connector=TextConnector(
            {"doc.txt": "Sensitive document text"},
            acl_ref_by_source={"doc.txt": "acl:finance"},
        ),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
        before_write=before_write,
        error_policy=error_policy,
    )


@pytest.mark.asyncio
async def test_authorized_document_passes_write_gate() -> None:
    store = RecordingStore()
    authz = _authz()
    audit = _audit()

    report = await _pipeline(
        store=store,
        extractor=EmptyExtractor(),
        authz=authz,
        audit=audit,
    ).run()

    assert report.documents_extracted == 1
    assert store.registration_attempts == [SOURCE]
    assert (
        await store.get_source_record(
            SOURCE.source_id,
            acl_ref=SOURCE.acl_ref,
        )
        == SOURCE
    )
    assert len(audit) == 1
    assert audit.verify_chain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_policy",
    [ErrorPolicy.FAIL_FAST, ErrorPolicy.SKIP_AND_LOG],
)
async def test_revoked_permission_prevents_all_registration_writes(
    error_policy: ErrorPolicy,
) -> None:
    store = RecordingStore()
    authz = _authz()
    audit = _audit()

    # Simulate permission revocation while extraction is underway.
    def revoke_during_extraction() -> None:
        authz.revoke(SUBJECT, VIEW, "acl:finance")

    pipeline = _pipeline(
        store=store,
        extractor=EmptyExtractor(
            on_extract=revoke_during_extraction,
        ),
        authz=authz,
        audit=audit,
        error_policy=error_policy,
    )

    with pytest.raises(
        ScopedReingestDeniedError,
        match="Scoped re-ingestion denied",
    ):
        await pipeline.run()

    assert store.registration_attempts == []
    assert (
        await store.get_source_record(
            SOURCE.source_id,
            acl_ref=SOURCE.acl_ref,
        )
        is None
    )
    assert len(audit) == 1
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_audit_failure_prevents_registration() -> None:
    class BrokenAudit(AuditEmitter):
        def emit(self, **kwargs: object):
            raise RuntimeError("audit unavailable")

    store = RecordingStore()

    pipeline = _pipeline(
        store=store,
        extractor=EmptyExtractor(),
        authz=_authz(),
        audit=BrokenAudit(signing_key=b"m3-3-broken-audit-test"),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await pipeline.run()

    assert store.registration_attempts == []


@pytest.mark.asyncio
async def test_ordinary_ingest_has_no_required_write_gate() -> None:
    store = RecordingStore()

    # No authorization callback: existing ingestion stays compatible.
    pipeline = IngestPipeline(
        connector=TextConnector({"doc.txt": "Public document"}),
        extractor=EmptyExtractor(),
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
    )

    report = await pipeline.run()

    assert report.documents_extracted == 1
    assert store.registration_attempts == [SourceRecord("doc.txt", None)]


class TwoEntitiesExtractor(EmptyExtractor):
    async def extract(
        self,
        input: ExtractionInput,
    ) -> ExtractionResult:
        provenance = Provenance(
            source_id=input.source_id,
            extractor_id=self.id,
            extractor_version="1",
            confidence=next(iter(Confidence)),
            confidence_score=0.95,
        )
        return ExtractionResult(
            entities=[
                Entity(
                    id="person-one",
                    type="Person",
                    canonical_name="Alice",
                    provenance=provenance,
                ),
                Entity(
                    id="person-two",
                    type="Person",
                    canonical_name="Bob",
                    provenance=provenance,
                ),
            ],
            facts=[],
            warnings=[],
        )


@pytest.mark.asyncio
async def test_revocation_between_entity_writes_blocks_second_write() -> None:
    authz = _authz()
    audit = _audit()

    class RevokingStore(RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.upserted: list[str] = []

        async def upsert_entity(
            self,
            entity: Entity,
            *,
            acl_ref: str | None = None,
        ) -> None:
            await super().upsert_entity(entity, acl_ref=acl_ref)
            self.upserted.append(entity.id)

            if len(self.upserted) == 1:
                # Access disappears immediately after the first write.
                authz.revoke(SUBJECT, VIEW, "acl:finance")

    store = RevokingStore()
    pipeline = _pipeline(
        store=store,
        extractor=TwoEntitiesExtractor(),
        authz=authz,
        audit=audit,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    )

    with pytest.raises(ScopedReingestDeniedError):
        await pipeline.run()

    # The first write had already committed. The second must not run.
    assert store.registration_attempts == [SOURCE]
    assert store.upserted == ["person-one"]
    assert await store.entity_acl("person-one") == "acl:finance"
    assert await store.entity_acl("person-two") is None

    # Initial check, first entity check, then revoked-access denial.
    assert len(audit) == 3
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_revocation_after_registration_blocks_entity_writes() -> None:
    authz = _authz()
    audit = _audit()

    class RevokingRegistrationStore(RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.upserted: list[str] = []

        async def register_source(self, record: SourceRecord) -> None:
            await super().register_source(record)
            authz.revoke(SUBJECT, VIEW, "acl:finance")

        async def upsert_entity(
            self,
            entity: Entity,
            *,
            acl_ref: str | None = None,
        ) -> None:
            self.upserted.append(entity.id)
            await super().upsert_entity(entity, acl_ref=acl_ref)

    store = RevokingRegistrationStore()

    with pytest.raises(ScopedReingestDeniedError):
        await _pipeline(
            store=store,
            extractor=TwoEntitiesExtractor(),
            authz=authz,
            audit=audit,
        ).run()

    assert store.registration_attempts == [SOURCE]
    assert store.upserted == []
    assert await store.entity_acl("person-one") is None
    assert audit.verify_chain()
