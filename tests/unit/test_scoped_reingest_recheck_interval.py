"""#126: bounded reuse of per-write authorization in scoped re-ingest."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.cli import app
from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.ingest.base import SourceDocument
from ontos.ingest.scoped_files import load_scoped_document
from ontos.ontology import load_ontology
from ontos.runtime.models import ArticleTwelveField, Confidence, Entity, Provenance
from ontos.runtime.scoped_execution import (
    MAX_RECHECK_INTERVAL_SECONDS,
    run_scoped_reingest,
)
from ontos.runtime.scoped_reingest import ScopedReingestDeniedError
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
SUBJECT = "user:operator"
IDENTITY = SourceRecord("selected.md", "acl:finance")
ENTITIES = 6


class CountingAuthz(InMemoryAuthz):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def check(self, subject: str, relation: str, object_: str) -> bool:
        self.calls += 1
        return await super().check(subject, relation, object_)

    async def list_authorized_objects(self, subject: str, relation: str) -> list[str]:
        self.calls += 1
        return await super().list_authorized_objects(subject, relation)


class CountingStore(NetworkxStore):
    def __init__(self, *, on_upsert: object = None) -> None:
        super().__init__()
        self.lookups = 0
        self.upserts: list[str] = []
        self.on_upsert = on_upsert

    async def get_source_record(
        self, source_id: str, *, acl_ref: str | None
    ) -> SourceRecord | None:
        self.lookups += 1
        return await super().get_source_record(source_id, acl_ref=acl_ref)

    async def upsert_entity(self, entity: Entity, *, acl_ref: str | None = None) -> None:
        await super().upsert_entity(entity, acl_ref=acl_ref)
        self.upserts.append(entity.id)
        if callable(self.on_upsert):
            self.on_upsert(len(self.upserts))


class ManyEntitiesExtractor:
    id = "recheck-interval-test"

    async def extract(self, input: ExtractionInput) -> ExtractionResult:
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
                    id=f"person-{index}",
                    type="Person",
                    canonical_name=f"Person {index}",
                    provenance=provenance,
                )
                for index in range(ENTITIES)
            ],
            facts=[],
            warnings=[],
        )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _authz() -> CountingAuthz:
    authz = CountingAuthz()
    authz.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    authz.grant(SUBJECT, VIEW, "acl:finance")
    return authz


def _results(audit: AuditEmitter) -> list[tuple[str, dict[str, object]]]:
    out = []
    for record in audit._chain:
        result = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])[0]["result"]
        arguments = json.loads(record.article12[ArticleTwelveField.TOOL_ARGUMENTS])
        out.append((result, arguments))
    return out


async def _run(
    tmp_path: Path,
    *,
    authz: CountingAuthz,
    store: CountingStore,
    audit: AuditEmitter,
    interval: float,
    clock: FakeClock | None = None,
):
    (tmp_path / "selected.md").write_text("Authorized test document", encoding="utf-8")
    await store.register_source(IDENTITY)

    async def loader(record: SourceRecord) -> SourceDocument:
        return await load_scoped_document(source_root=tmp_path, identity=record)

    return await run_scoped_reingest(
        authz=authz,
        audit=audit,
        subject=SUBJECT,
        sources=[IDENTITY],
        registry=store,
        load_source=loader,
        extractor=ManyEntitiesExtractor(),
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
        recheck_interval_seconds=interval,
        clock=clock or FakeClock(),
    )


@pytest.mark.asyncio
async def test_interval_reduces_authorization_calls(tmp_path: Path) -> None:
    per_write_authz, per_write_store = _authz(), CountingStore()
    await _run(
        tmp_path,
        authz=per_write_authz,
        store=per_write_store,
        audit=AuditEmitter(b"k"),
        interval=0,
    )

    cached_authz, cached_store = _authz(), CountingStore()
    await _run(
        tmp_path,
        authz=cached_authz,
        store=cached_store,
        audit=AuditEmitter(b"k"),
        interval=60,
    )

    assert len(per_write_store.upserts) == len(cached_store.upserts) == ENTITIES
    # Default: two authz calls and one registry lookup per mutation.
    # With a window: one recheck covers every write in the document.
    per_write_mutation_calls = per_write_authz.calls - cached_authz.calls
    assert per_write_mutation_calls >= 2 * (ENTITIES - 1)
    assert per_write_store.lookups - cached_store.lookups >= ENTITIES - 1


@pytest.mark.asyncio
async def test_default_still_catches_revocation_on_the_next_write(tmp_path: Path) -> None:
    authz = _authz()

    def revoke_after_first(count: int) -> None:
        if count == 1:
            authz.revoke(SUBJECT, VIEW, "acl:finance")

    store = CountingStore(on_upsert=revoke_after_first)

    with pytest.raises(ScopedReingestDeniedError):
        await _run(tmp_path, authz=authz, store=store, audit=AuditEmitter(b"k"), interval=0)

    assert len(store.upserts) == 1


@pytest.mark.asyncio
async def test_revocation_is_caught_once_the_window_expires(tmp_path: Path) -> None:
    authz = _authz()
    clock = FakeClock()

    def revoke_then_advance(count: int) -> None:
        if count == 1:
            authz.revoke(SUBJECT, VIEW, "acl:finance")
        if count == 3:
            clock.now += 30.0

    store = CountingStore(on_upsert=revoke_then_advance)
    audit = AuditEmitter(b"k")

    with pytest.raises(ScopedReingestDeniedError):
        await _run(tmp_path, authz=authz, store=store, audit=audit, interval=30, clock=clock)

    # Writes inside the window proceed; the first write after it is denied.
    assert len(store.upserts) == 3
    assert [result for result, _ in _results(audit)][-2:] == ["deny", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interval",
    [-1.0, math.inf, math.nan, MAX_RECHECK_INTERVAL_SECONDS + 1, True],
)
async def test_invalid_interval_is_audited_and_loads_nothing(
    tmp_path: Path, interval: float
) -> None:
    audit = AuditEmitter(b"k")
    store = CountingStore()

    with pytest.raises(ValueError):
        await _run(tmp_path, authz=_authz(), store=store, audit=audit, interval=interval)

    assert [result for result, _ in _results(audit)] == ["invalid_request"]
    assert store.upserts == []


@pytest.mark.asyncio
async def test_audit_records_the_interval_only_when_relaxed(tmp_path: Path) -> None:
    relaxed = AuditEmitter(b"k")
    await _run(tmp_path, authz=_authz(), store=CountingStore(), audit=relaxed, interval=15)
    result, arguments = _results(relaxed)[-1]
    assert result == "completed"
    assert arguments["recheck_interval_seconds"] == 15.0

    strict = AuditEmitter(b"k")
    await _run(tmp_path, authz=_authz(), store=CountingStore(), audit=strict, interval=0)
    assert all("recheck_interval_seconds" not in arguments for _, arguments in _results(strict))


@pytest.mark.parametrize("value", ["-5", "abc", "301", "inf"])
def test_cli_rejects_invalid_recheck_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "recheck-cli-key")
    monkeypatch.setenv("ONTOS_REINGEST_RECHECK_SECONDS", value)

    result = CliRunner().invoke(
        app,
        [
            "reingest",
            "--source-dir",
            str(tmp_path),
            "--source-id",
            "selected.md",
            "--ontology",
            str(STARTER),
            "--acting-on-behalf-of",
            SUBJECT,
            "--storage-path",
            str(tmp_path / "store.pkl"),
        ],
    )

    assert result.exit_code == 2
    assert "Invalid ONTOS_REINGEST_RECHECK_SECONDS" in result.output
