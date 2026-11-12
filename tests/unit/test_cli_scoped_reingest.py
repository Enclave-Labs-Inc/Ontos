"""M3-3: operator CLI must fail closed before source loading."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontos.audit.postgres import PostgresAuditEmitter
from ontos.cli import app

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})
STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _args(tmp_path: Path) -> list[str]:
    return [
        "reingest",
        "--source-dir",
        str(tmp_path),
        "--source-id",
        "restricted.md",
        "--acl-ref",
        "acl:finance",
        "--ontology",
        str(STARTER),
        "--acting-on-behalf-of",
        "user:operator",
        "--storage-path",
        str(tmp_path / "store.pkl"),
    ]


@pytest.fixture
def audit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}"
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", url)
    monkeypatch.setenv("ONTOS_REINGEST_OPERATOR_SUBJECT", "user:operator")
    monkeypatch.setenv("ONTOS_REINGEST_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m3-3-cli-test-signing-key",
    )
    monkeypatch.setenv("ONTOS_AUTHZ_BACKEND", "none")
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)
    return url


def _audit_count(url: str) -> int:
    async def read() -> int:
        audit = PostgresAuditEmitter.from_url(
            url,
            signing_key_env="ONTOS_AUDIT_SIGNING_KEY",
        )
        try:
            await audit.initialize()
            assert await audit.verify_chain_async()
            return await audit.count_async()
        finally:
            await audit.close()

    return asyncio.run(read())


def test_reingest_help_exposes_explicit_scope() -> None:
    result = runner.invoke(app, ["reingest", "--help"])
    assert result.exit_code == 0, result.stdout
    for option in (
        "--source-dir",
        "--source-id",
        "--acl-ref",
        "--ontology",
        "--acting-on-behalf-of",
        "--storage-path",
        "--ollama-model",
    ):
        assert option in result.stdout


def test_missing_operator_permissions_block_source_loading(
    tmp_path: Path,
    audit_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.ingest.scoped_files

    (tmp_path / "restricted.md").write_text(
        "SECRET DOCUMENT CONTENT",
        encoding="utf-8",
    )

    opened: list[str] = []

    async def forbidden_loader(**kwargs: object):
        opened.append("opened")
        raise AssertionError("Unauthorized source loading")

    monkeypatch.setattr(
        ontos.ingest.scoped_files,
        "load_scoped_document",
        forbidden_loader,
    )

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 1, result.stdout
    assert opened == []
    assert "SECRET DOCUMENT CONTENT" not in result.stdout
    assert "restricted.md" not in result.stdout
    assert _audit_count(audit_config) >= 1


def test_missing_signing_key_blocks_before_loading(
    tmp_path: Path,
    audit_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.ingest.scoped_files

    monkeypatch.delenv("ONTOS_AUDIT_SIGNING_KEY")

    calls: list[str] = []

    async def forbidden_loader(**kwargs: object):
        calls.append("opened")
        raise AssertionError("Source loading must not happen")

    monkeypatch.setattr(
        ontos.ingest.scoped_files,
        "load_scoped_document",
        forbidden_loader,
    )

    result = runner.invoke(app, _args(tmp_path))

    assert result.exit_code == 2
    assert calls == []


def test_remote_ollama_endpoint_is_refused(
    tmp_path: Path,
    audit_config: str,
) -> None:
    result = runner.invoke(
        app,
        _args(tmp_path) + ["--ollama-url", "https://external.example/api"],
    )

    assert result.exit_code != 0
    assert "loopback" in result.output.lower()
