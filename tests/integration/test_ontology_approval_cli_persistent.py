"""M3-2: approval CLI against persistent candidate and audit stores."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from typer.testing import CliRunner

from ontos.audit.base import lookup_record
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.cli import app
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.ontology import load_ontology
from ontos.runtime.models import ArticleTwelveField
from ontos.storage.networkx_store import NetworkxStore

runner = CliRunner()

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"

SUBJECT = "user:operator"
ACL = "acl:legal"


def _seed_candidates(path: Path) -> None:
    ontology = load_ontology(STARTER)

    assert not ontology.is_valid_pattern("Person", "acquired", "Company")

    candidate = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="person-1",
            subject_type="Person",
            subject_canonical_name="Person One",
            predicate="acquired",
            object_id="company-1",
            object_type="Company",
            object_canonical_name="Company One",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="integration-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            confidence_score=0.9,
        ),
        acl_ref=ACL,
    )

    async def seed() -> None:
        store = NetworkxStore(path=path)
        try:
            await store.add_candidates([candidate])
        finally:
            await store.close()

    asyncio.run(seed())


def _change_file(tmp_path: Path) -> Path:
    path = tmp_path / "change.yaml"
    path.write_text(
        "action: add_pattern\nsubject_type: Person\npredicate: acquired\nobject_type: Company\n",
        encoding="utf-8",
    )
    return path


def _args(tmp_path: Path, *, base_version: str = "0.1") -> list[str]:
    return [
        "ontology",
        "approve",
        "--acting-on-behalf-of",
        SUBJECT,
        "--ontology",
        str(STARTER),
        "--base-version",
        base_version,
        "--new-version",
        "0.2",
        "--change-file",
        str(tmp_path / "change.yaml"),
        "--acl-ref",
        ACL,
        "--ontology-output",
        str(tmp_path / "ontology-0.2.yaml"),
        "--migration-output",
        str(tmp_path / "migration-0.1-to-0.2.yaml"),
        "--storage-path",
        str(tmp_path / "candidates.pkl"),
    ]


def _configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authz: InMemoryAuthz,
) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}"
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", url)
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "m3-2-persistent-integration-key")
    monkeypatch.setattr(
        "ontos.runtime.server.build_authz",
        lambda cfg: authz,
    )
    return url


def _authz(*, approver: bool = True, view: bool = True) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    if approver:
        authz.grant(SUBJECT, ONTOLOGY_APPROVER, "ontology")
    if view:
        authz.grant(SUBJECT, VIEW, ACL)
    return authz


async def _read_persisted_approval(url: str, query_id: UUID):
    audit = PostgresAuditEmitter.from_url(url)
    try:
        await audit.initialize()
        return await lookup_record(audit, query_id)
    finally:
        await audit.close()


def test_cli_publishes_with_genuine_persisted_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_candidates(tmp_path / "candidates.pkl")
    _change_file(tmp_path)
    url = _configure(tmp_path, monkeypatch, _authz())

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 0, result.output + repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["activated"] is False
    assert payload["base_version"] == "0.1"
    assert payload["new_version"] == "0.2"

    ontology_file = tmp_path / "ontology-0.2.yaml"
    migration_file = tmp_path / "migration-0.1-to-0.2.yaml"

    assert ontology_file.exists()
    assert migration_file.exists()

    updated = load_ontology(ontology_file)
    assert updated.version == "0.2"
    assert updated.is_valid_pattern("Person", "acquired", "Company")

    migration = yaml.safe_load(migration_file.read_text(encoding="utf-8"))
    assert migration["approval_query_id"] == payload["approval_query_id"]

    query_id = UUID(payload["approval_query_id"])
    assert query_id.int != 0

    record = asyncio.run(_read_persisted_approval(url, query_id))
    assert record is not None
    assert record.query_id == query_id
    assert record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_approve"
    assert json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS]) == [
        {
            "policy": "ontology_approval",
            "result": "approval_granted",
        }
    ]


def test_cli_refuses_subject_without_approver_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_candidates(tmp_path / "candidates.pkl")
    _change_file(tmp_path)
    _configure(tmp_path, monkeypatch, _authz(approver=False))

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code != 0
    assert "Ontology approval denied" in result.output
    assert not (tmp_path / "ontology-0.2.yaml").exists()
    assert not (tmp_path / "migration-0.1-to-0.2.yaml").exists()


def test_cli_hidden_evidence_is_indistinguishable_from_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_candidates(tmp_path / "candidates.pkl")
    _change_file(tmp_path)
    _configure(tmp_path, monkeypatch, _authz(view=False))

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code != 0
    assert "No such suggestion" in result.output
    assert not (tmp_path / "ontology-0.2.yaml").exists()
    assert not (tmp_path / "migration-0.1-to-0.2.yaml").exists()


def test_cli_rejects_stale_base_version_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_candidates(tmp_path / "candidates.pkl")
    _change_file(tmp_path)
    _configure(tmp_path, monkeypatch, _authz())

    result = runner.invoke(app, _args(tmp_path, base_version="0.0"))

    assert result.exit_code != 0
    assert "Invalid approval request" in result.output
    assert not (tmp_path / "ontology-0.2.yaml").exists()
    assert not (tmp_path / "migration-0.1-to-0.2.yaml").exists()
