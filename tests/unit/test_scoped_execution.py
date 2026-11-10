"""M3-3: secure scoped re-ingestion execution without CLI exposure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.ingest.scoped_files import load_scoped_document
from ontos.ontology import load_ontology
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.scoped_execution import run_scoped_reingest
from ontos.runtime.scoped_reingest import ScopedReingestDeniedError
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
