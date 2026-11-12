"""#124: `ontos ontology affected-sources` CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from typer.testing import CliRunner

import ontos.runtime.server
from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.cli import app
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.migration.approval_audit import sign_migration_record
from ontos.migration.records import AddPatternChange, MigrationRecord
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

SUBJECT = "user:operator"
SIGNING_KEY = "affected-sources-cli-signing-key"


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


def _write_record(path: Path, *, signing_key: str) -> None:
    record = sign_migration_record(
        record=MigrationRecord(
            schema_version=1,
            ontology_id="ontos.starter",
            from_version="0.1",
            to_version="0.2",
            migration_kind="identity",
            approval_query_id=UUID(int=7),
            approved_changes=[
                AddPatternChange(
                    action="add_pattern",
                    subject_type="Company",
                    predicate="acquired",
                    object_type="Company",
                )
            ],
        ),
        audit=AuditEmitter(signing_key.encode()),
    )
    path.write_text(yaml.safe_dump(record.model_dump(mode="json"), sort_keys=False))


def _seed_store(path: Path) -> None:
    async def seed() -> None:
        store = NetworkxStore(path=path)
        try:
            await store.add_candidates(
                [_candidate("deal.md", "acl:legal"), _candidate("legacy.md", None)]
            )
            await store.register_source(SourceRecord("deal.md", "acl:legal"))
        finally:
            await store.close()

    asyncio.run(seed())


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InMemoryAuthz:
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)

    backend = InMemoryAuthz()
    backend.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    backend.grant(SUBJECT, VIEW, "acl:legal")
    monkeypatch.setattr(ontos.runtime.server, "build_authz", lambda config: backend)
    return backend


def _args(tmp_path: Path) -> list[str]:
    return [
        "ontology",
        "affected-sources",
        "--acting-on-behalf-of",
        SUBJECT,
        "--migration-record",
        str(tmp_path / "migration.yaml"),
        "--storage-path",
        str(tmp_path / "store.pkl"),
    ]


def test_lists_registered_and_unregistered_sources(tmp_path: Path, cli_env: InMemoryAuthz) -> None:
    _seed_store(tmp_path / "store.pkl")
    _write_record(tmp_path / "migration.yaml", signing_key=SIGNING_KEY)

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["to_version"] == "0.2"
    assert payload["registered"] == [{"source_id": "deal.md", "acl_ref": "acl:legal"}]
    assert payload["unregistered"] == [{"source_id": "legacy.md", "acl_ref": None}]


def test_record_signed_by_another_key_exits_2(tmp_path: Path, cli_env: InMemoryAuthz) -> None:
    _seed_store(tmp_path / "store.pkl")
    _write_record(tmp_path / "migration.yaml", signing_key="not-this-deployment")

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 2
    assert "not a verified approval" in result.output
    assert "deal.md" not in result.output


def test_non_approver_is_denied(tmp_path: Path, cli_env: InMemoryAuthz) -> None:
    _seed_store(tmp_path / "store.pkl")
    _write_record(tmp_path / "migration.yaml", signing_key=SIGNING_KEY)
    cli_env.revoke(SUBJECT, ONTOLOGY_APPROVER, "ontology")

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 1
    assert "denied" in result.output
    assert "deal.md" not in result.output


def test_requires_explicit_signing_key(
    tmp_path: Path, cli_env: InMemoryAuthz, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_record(tmp_path / "migration.yaml", signing_key=SIGNING_KEY)
    monkeypatch.delenv("ONTOS_AUDIT_SIGNING_KEY")

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 2
    assert "signing key required" in result.output


def test_invalid_record_file_exits_2(tmp_path: Path, cli_env: InMemoryAuthz) -> None:
    (tmp_path / "migration.yaml").write_text("not: a migration\n")

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 2
    assert "Invalid migration record" in result.output
