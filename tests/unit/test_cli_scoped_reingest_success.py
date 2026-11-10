"""M3-3: successful scoped CLI execution with a stub extractor.

No real Ollama calls or customer documents are used.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import ontos.extraction
import ontos.llm
import ontos.runtime.server
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.cli import app
from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.runtime.models import ArticleTwelveField, AuditRecord
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"

PDF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs" / "sample-text.pdf"

SUBJECT = "user:operator"


class StubLLM:
    id = "test-local-ollama"

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


def _cli_args(
    root: Path,
    source_id: str,
    acl_ref: str | None,
) -> list[str]:
    args = [
        "reingest",
        "--source-dir",
        str(root),
        "--source-id",
        source_id,
        "--ontology",
        str(STARTER),
        "--acting-on-behalf-of",
        SUBJECT,
        "--storage-path",
        str(root / "store.pkl"),
    ]
    if acl_ref is not None:
        args.extend(["--acl-ref", acl_ref])
    return args


def _seed_source(
    path: Path,
    identity: SourceRecord,
) -> None:
    async def seed() -> None:
        store = NetworkxStore(path=path)
        try:
            await store.register_source(identity)
        finally:
            await store.close()

    asyncio.run(seed())


def _read_audit_records(url: str) -> list[AuditRecord]:
    async def read() -> list[AuditRecord]:
        from sqlalchemy import select

        from ontos.audit.postgres import (
            _audit_table,
            _record_from_row,
        )

        audit = PostgresAuditEmitter.from_url(
            url,
            signing_key_env="ONTOS_AUDIT_SIGNING_KEY",
        )
        try:
            await audit.initialize()
            assert await audit.verify_chain_async()

            async with audit._engine.connect() as connection:
                rows = await connection.execute(
                    select(_audit_table).order_by(_audit_table.c.ordinal)
                )
                return [_record_from_row(dict(row)) for row in rows.mappings()]
        finally:
            await audit.close()

    return asyncio.run(read())


def _decisions(records: list[AuditRecord]) -> list[str]:
    decisions: list[str] = []
    for record in records:
        parsed = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
        decisions.append(parsed[0]["result"])
    return decisions


@pytest.fixture
def setup_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, InMemoryAuthz, list[str]]:
    url = "sqlite+aiosqlite:///" + str(tmp_path / "audit.sqlite")

    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", url)
    monkeypatch.setenv("ONTOS_REINGEST_OPERATOR_SUBJECT", SUBJECT)
    monkeypatch.setenv("ONTOS_REINGEST_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m3-3-success-test-signing-key",
    )
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)

    backend = InMemoryAuthz()
    backend.grant(
        SUBJECT,
        ONTOLOGY_APPROVER,
        "ontology",
    )

    seen: list[str] = []

    class StubExtractor:
        id = "scoped-cli-stub-extractor"

        def __init__(
            self,
            llm: object,
            ontology: object,
        ) -> None:
            pass

        async def extract(
            self,
            input: ExtractionInput,
        ) -> ExtractionResult:
            seen.append(input.source_id)
            return ExtractionResult(
                facts=[],
                warnings=[],
            )

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        lambda config: backend,
    )
    monkeypatch.setattr(
        ontos.llm,
        "OllamaBackend",
        StubLLM,
    )
    monkeypatch.setattr(
        ontos.extraction,
        "LlmExtractor",
        StubExtractor,
    )

    return url, backend, seen


def test_authorized_registered_text_source_completes(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
) -> None:
    url, backend, seen = setup_cli

    identity = SourceRecord(
        "selected.md",
        "acl:finance",
    )
    backend.grant(SUBJECT, VIEW, "acl:finance")

    (tmp_path / "selected.md").write_text(
        "SECRET SELECTED DOCUMENT",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.md").write_text(
        "SECRET UNRELATED DOCUMENT",
        encoding="utf-8",
    )

    _seed_source(tmp_path / "store.pkl", identity)

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            identity.source_id,
            identity.acl_ref,
        ),
    )

    assert result.exit_code == 0, result.output + repr(result.exception)

    response = json.loads(result.stdout.strip())

    assert response["completed"] is True
    assert response["documents_seen"] == 1
    assert response["documents_extracted"] == 1
    assert response["facts_written"] == 0
    assert seen == ["selected.md"]

    records = _read_audit_records(url)
    assert len(records) >= 2
    assert _decisions(records)[-1] == "completed"

    audit_json = json.dumps([record.model_dump(mode="json") for record in records])

    for secret in (
        "SECRET SELECTED DOCUMENT",
        "SECRET UNRELATED DOCUMENT",
        "selected.md",
        "unrelated.md",
        "acl:finance",
    ):
        assert secret not in audit_json


def test_registered_acl_mismatch_is_denied(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, backend, seen = setup_cli

    # Registered under HR, but operator requests finance.
    _seed_source(
        tmp_path / "store.pkl",
        SourceRecord("restricted.md", "acl:hr"),
    )

    backend.grant(SUBJECT, VIEW, "acl:finance")

    (tmp_path / "restricted.md").write_text(
        "PRIVATE DOCUMENT",
        encoding="utf-8",
    )

    import ontos.ingest.scoped_files

    opened: list[str] = []
    original_loader = ontos.ingest.scoped_files.load_scoped_document

    async def spy_loader(**kwargs: Any):
        opened.append("opened")
        return await original_loader(**kwargs)

    monkeypatch.setattr(
        ontos.ingest.scoped_files,
        "load_scoped_document",
        spy_loader,
    )

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            "restricted.md",
            "acl:finance",
        ),
    )

    assert result.exit_code == 1
    assert opened == []
    assert seen == []
    assert "PRIVATE DOCUMENT" not in result.output

    records = _read_audit_records(url)
    assert records
    assert "completed" not in _decisions(records)


def test_authorized_public_source_completes(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
) -> None:
    url, _, seen = setup_cli

    identity = SourceRecord("public.txt", None)
    (tmp_path / "public.txt").write_text(
        "Public test content",
        encoding="utf-8",
    )

    _seed_source(tmp_path / "store.pkl", identity)

    result = runner.invoke(
        app,
        _cli_args(tmp_path, "public.txt", None),
    )

    assert result.exit_code == 0, result.output
    assert seen == ["public.txt"]
    assert _decisions(_read_audit_records(url))[-1] == "completed"


def test_authorized_pdf_source_completes_locally(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
) -> None:
    url, backend, seen = setup_cli

    identity = SourceRecord(
        "document.pdf",
        "acl:legal",
    )
    backend.grant(SUBJECT, VIEW, "acl:legal")

    shutil.copyfile(
        PDF_FIXTURE,
        tmp_path / "document.pdf",
    )
    _seed_source(tmp_path / "store.pkl", identity)

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            identity.source_id,
            identity.acl_ref,
        ),
    )

    assert result.exit_code == 0, result.output
    assert seen == ["document.pdf"]
    assert _decisions(_read_audit_records(url))[-1] == "completed"


def test_persistence_failure_never_reports_completion(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, backend, seen = setup_cli

    identity = SourceRecord("selected.md", "acl:finance")
    backend.grant(SUBJECT, VIEW, "acl:finance")

    (tmp_path / "selected.md").write_text(
        "PRIVATE TEST DOCUMENT",
        encoding="utf-8",
    )
    _seed_source(tmp_path / "store.pkl", identity)

    class FailingCloseStore(NetworkxStore):
        async def close(self) -> None:
            raise OSError("PRIVATE INTERNAL STORAGE ERROR")

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_store",
        lambda config: FailingCloseStore(path=tmp_path / "store.pkl"),
    )

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            identity.source_id,
            identity.acl_ref,
        ),
    )

    assert result.exit_code == 1, result.output
    assert seen == ["selected.md"]

    # Neither private document text nor internal error details
    # may appear in the CLI response.
    assert "PRIVATE TEST DOCUMENT" not in result.output
    assert "PRIVATE INTERNAL STORAGE ERROR" not in result.output
    assert '"completed": true' not in result.output.lower()

    records = _read_audit_records(url)
    assert _decisions(records)[-1] == "persistence_failed"
    assert "completed" not in _decisions(records)

    audit_json = json.dumps([record.model_dump(mode="json") for record in records])
    assert "PRIVATE INTERNAL STORAGE ERROR" not in audit_json
    assert "PRIVATE TEST DOCUMENT" not in audit_json


@pytest.mark.parametrize(
    "root_mode",
    ["missing", "mismatch"],
)
def test_untrusted_source_root_blocks_document_loading(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    root_mode: str,
) -> None:
    url, backend, seen = setup_cli

    identity = SourceRecord(
        "restricted.md",
        "acl:finance",
    )
    backend.grant(
        SUBJECT,
        VIEW,
        "acl:finance",
    )

    (tmp_path / "restricted.md").write_text(
        "PRIVATE FINANCIAL DOCUMENT",
        encoding="utf-8",
    )
    _seed_source(tmp_path / "store.pkl", identity)

    if root_mode == "missing":
        monkeypatch.delenv(
            "ONTOS_REINGEST_SOURCE_ROOT",
            raising=False,
        )
    else:
        other_root = tmp_path / "different-root"
        other_root.mkdir()
        monkeypatch.setenv(
            "ONTOS_REINGEST_SOURCE_ROOT",
            str(other_root),
        )

    import ontos.ingest.scoped_files

    opened: list[str] = []

    async def forbidden_loader(**kwargs: Any):
        opened.append("opened")
        raise AssertionError("Untrusted directory must never be read")

    monkeypatch.setattr(
        ontos.ingest.scoped_files,
        "load_scoped_document",
        forbidden_loader,
    )

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            identity.source_id,
            identity.acl_ref,
        ),
    )

    assert result.exit_code == 1, result.output
    assert opened == []
    assert seen == []
    assert "PRIVATE FINANCIAL DOCUMENT" not in result.output

    records = _read_audit_records(url)
    assert _decisions(records)[-1] == "source_root_untrusted"
    assert "completed" not in _decisions(records)


@pytest.mark.parametrize(
    "identity_mode",
    ["missing", "mismatch"],
)
def test_untrusted_operator_identity_blocks_before_authorization(
    tmp_path: Path,
    setup_cli: tuple[str, InMemoryAuthz, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    identity_mode: str,
) -> None:
    url, backend, seen = setup_cli

    identity = SourceRecord(
        "restricted.md",
        "acl:finance",
    )
    backend.grant(SUBJECT, VIEW, "acl:finance")

    (tmp_path / "restricted.md").write_text(
        "PRIVATE OPERATOR TEST DOCUMENT",
        encoding="utf-8",
    )
    _seed_source(tmp_path / "store.pkl", identity)

    if identity_mode == "missing":
        monkeypatch.delenv(
            "ONTOS_REINGEST_OPERATOR_SUBJECT",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "ONTOS_REINGEST_OPERATOR_SUBJECT",
            "user:different-operator",
        )

    authorization_attempts: list[str] = []

    def forbidden_authz(config: object) -> None:
        authorization_attempts.append("attempted")
        raise AssertionError("Untrusted identity reached authorization")

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        forbidden_authz,
    )

    result = runner.invoke(
        app,
        _cli_args(
            tmp_path,
            identity.source_id,
            identity.acl_ref,
        ),
    )

    assert result.exit_code == 1, result.output
    assert authorization_attempts == []
    assert seen == []
    assert "PRIVATE OPERATOR TEST DOCUMENT" not in result.output

    records = _read_audit_records(url)
    assert _decisions(records)[-1] == "operator_identity_untrusted"
    assert "completed" not in _decisions(records)

    # The audit must capture the caller's claim, not attribute
    # the rejected request to the configured trusted subject.
    assert records[-1].article12[ArticleTwelveField.ACTING_ON_BEHALF_OF] == SUBJECT

    audit_json = json.dumps([record.model_dump(mode="json") for record in records])
    assert "PRIVATE OPERATOR TEST DOCUMENT" not in audit_json
