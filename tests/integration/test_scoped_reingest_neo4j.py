"""M3-3: scoped re-ingestion against a real Neo4j container."""

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
from ontos.runtime.scoped_reingest import ScopedReingestDeniedError
from ontos.storage.neo4j_store import Neo4jStore
from ontos.storage.sources import SourceRecord

# Reuse the real Docker fixtures from the existing Neo4j suite.
from tests.integration.test_neo4j_store import (
    neo4j_container as neo4j_container,
)
from tests.integration.test_neo4j_store import (
    store as store,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SUBJECT = "user:operator"

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def authorized(*acls: str) -> InMemoryAuthz:
    backend = InMemoryAuthz()
    backend.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")

    for acl in acls:
        backend.grant(SUBJECT, VIEW, acl)

    return backend


class StubExtractor:
    id = "m3-3-neo4j-test-extractor"

    def __init__(
        self,
        entity_id: str,
        *,
        revoke: InMemoryAuthz | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.revoke = revoke
        self.seen: list[str] = []

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
                    id=self.entity_id,
                    type="Person",
                    canonical_name="Scoped Test Employee",
                    provenance=provenance,
                )
            ],
            facts=[],
            warnings=[],
        )


async def read_entity(
    store: Neo4jStore,
    entity_id: str,
) -> dict[str, str] | None:
    async with store._driver.session(database=store._database) as session:
        result = await session.run(
            """
            MATCH (e:Entity {id: $entity_id})
            RETURN e.id AS id,
                   e.acl_ref AS acl_ref,
                   e.canonical_name AS canonical_name
            """,
            entity_id=entity_id,
        )

        row = await result.single()
        return dict(row) if row is not None else None


def audit_decisions(audit: AuditEmitter) -> list[str]:
    decisions: list[str] = []

    for record in audit._chain:
        parsed = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        decisions.append(parsed[0]["result"])

    return decisions


@pytest.mark.asyncio
async def test_authorized_scoped_reingest_writes_entity(
    store: Neo4jStore,
    tmp_path: Path,
) -> None:
    (tmp_path / "selected.md").write_text(
        "Selected private financial document",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.md").write_text(
        "Unrelated confidential document",
        encoding="utf-8",
    )

    identity = SourceRecord(
        "selected.md",
        "acl:finance",
    )
    await store.register_source(identity)

    loaded: list[str] = []

    async def loader(
        record: SourceRecord,
    ) -> SourceDocument:
        loaded.append(record.source_id)
        return await load_scoped_document(
            source_root=tmp_path,
            identity=record,
        )

    extractor = StubExtractor("m3-3-authorized-entity")
    audit = AuditEmitter(signing_key=b"m3-3-real-neo4j-authorized")

    report = await run_scoped_reingest(
        authz=authorized("acl:finance"),
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

    entity = await read_entity(
        store,
        extractor.entity_id,
    )

    assert entity is not None
    assert entity["id"] == extractor.entity_id
    assert entity["acl_ref"] == "acl:finance"
    assert entity["canonical_name"] == "Scoped Test Employee"

    assert (
        await store.get_source_record(
            identity.source_id,
            acl_ref=identity.acl_ref,
        )
        == identity
    )

    assert audit.verify_chain()
    assert audit_decisions(audit)[-1] == "completed"

    audit_json = json.dumps([record.model_dump(mode="json") for record in audit._chain])

    for secret in (
        "selected.md",
        "unrelated.md",
        "Selected private financial document",
        "acl:finance",
    ):
        assert secret not in audit_json


@pytest.mark.asyncio
async def test_acl_mismatch_blocks_loading_and_writes(
    store: Neo4jStore,
) -> None:
    await store.register_source(SourceRecord("restricted.md", "acl:hr"))

    requested = SourceRecord(
        "restricted.md",
        "acl:finance",
    )

    loaded: list[str] = []

    async def forbidden_loader(
        record: SourceRecord,
    ) -> SourceDocument:
        loaded.append(record.source_id)
        raise AssertionError("Unauthorized file was loaded")

    extractor = StubExtractor("m3-3-mismatched-entity")
    audit = AuditEmitter(signing_key=b"m3-3-real-neo4j-mismatch")

    with pytest.raises(ScopedReingestDeniedError):
        await run_scoped_reingest(
            authz=authorized("acl:finance"),
            audit=audit,
            subject=SUBJECT,
            sources=[requested],
            registry=store,
            load_source=forbidden_loader,
            extractor=extractor,
            resolver=None,
            store=store,
            ontology=load_ontology(STARTER),
        )

    assert loaded == []
    assert extractor.seen == []

    assert (
        await read_entity(
            store,
            extractor.entity_id,
        )
        is None
    )

    assert "completed" not in audit_decisions(audit)
    assert audit.verify_chain()


@pytest.mark.asyncio
async def test_revocation_blocks_neo4j_entity_write(
    store: Neo4jStore,
    tmp_path: Path,
) -> None:
    (tmp_path / "restricted.md").write_text(
        "Private financial information",
        encoding="utf-8",
    )

    identity = SourceRecord(
        "restricted.md",
        "acl:finance",
    )
    await store.register_source(identity)

    backend = authorized("acl:finance")
    extractor = StubExtractor(
        "m3-3-revoked-entity",
        revoke=backend,
    )
    audit = AuditEmitter(signing_key=b"m3-3-real-neo4j-revocation")

    async def loader(
        record: SourceRecord,
    ) -> SourceDocument:
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

    assert (
        await read_entity(
            store,
            extractor.entity_id,
        )
        is None
    )

    assert (
        await store.get_source_record(
            identity.source_id,
            acl_ref=identity.acl_ref,
        )
        == identity
    )

    assert "deny" in audit_decisions(audit)
    assert audit_decisions(audit)[-1] == "failed"
    assert audit.verify_chain()
