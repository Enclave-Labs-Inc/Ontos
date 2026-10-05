"""M5.c — CLI tests via typer's CliRunner. Does not hit real Ollama / Postgres."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontos.cli import app

# Force a wide "terminal" so Typer/Rich doesn't wrap option names.
# On CI (narrow TTY) --source-dir would render as --source-\ndir and
# the naive substring check would miss it. Env goes through to Click.
runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


def test_help_lists_all_top_level_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("serve", "ingest", "query", "audit"):
        assert command in result.stdout


def test_serve_help() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.stdout
    assert "--port" in result.stdout


def test_ingest_help_documents_ontology_and_source_options() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "--source-dir" in result.stdout
    assert "--ontology" in result.stdout
    assert "--ollama-model" in result.stdout
    assert "--error-policy" in result.stdout
    assert "--pdf-backend" in result.stdout
    assert "--llama-api-key-env" in result.stdout


def test_query_help_requires_ontology() -> None:
    result = runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert "--ontology" in result.stdout


def test_audit_verify_help() -> None:
    result = runner.invoke(app, ["audit", "verify", "--help"])
    assert result.exit_code == 0
    assert "--db-url" in result.stdout


def test_audit_verify_without_db_url_exits_with_helpful_message(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ONTOS_AUDIT_DB_URL", raising=False)
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 2
    assert "cannot verify" in result.stdout.lower()


def test_ingest_missing_source_dir_fails_loud(tmp_path: Path) -> None:
    # Typer's own exists=True validation catches this before the command runs.
    result = runner.invoke(
        app,
        [
            "ingest",
            "--source-dir",
            str(tmp_path / "nope"),
            "--ontology",
            str(STARTER_PATH),
        ],
    )
    assert result.exit_code != 0


def test_read_source_dir_only_picks_txt_and_md(tmp_path: Path) -> None:
    """`_read_source_dir` is now the TEXT-only slice; PDFs are routed separately."""
    from ontos.cli.main import _read_source_dir

    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.md").write_text("bravo")
    (tmp_path / "c.pdf").write_text("charlie")  # handled by the PDF slice, not here
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.md").write_text("delta")

    docs = _read_source_dir(tmp_path)
    assert set(docs.keys()) == {"a.txt", "b.md", "sub/d.md"}
    assert "c.pdf" not in docs
    assert docs["a.txt"] == "alpha"
    assert docs["sub/d.md"] == "delta"


def test_collect_pdf_paths_finds_pdfs(tmp_path: Path) -> None:
    from ontos.cli.main import _collect_pdf_paths

    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.PDF").write_bytes(b"%PDF-1.4\n")

    pdfs = _collect_pdf_paths(tmp_path)
    assert len(pdfs) == 2
    assert all(p.suffix.lower() == ".pdf" for p in pdfs)


def test_build_source_connector_text_only(tmp_path: Path) -> None:
    from ontos.cli.main import _build_source_connector
    from ontos.ingest import MultiConnector, TextConnector

    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.md").write_text("bravo")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend=None, llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert isinstance(connector, MultiConnector)
    assert count == 2
    assert len(connector.sub_connectors) == 1
    assert isinstance(connector.sub_connectors[0], TextConnector)


def test_build_source_connector_pdf_without_backend_exits(tmp_path: Path) -> None:
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(_typer.Exit) as exc:
        _build_source_connector(tmp_path, pdf_backend=None, llama_api_key_env="LLAMA_CLOUD_API_KEY")
    assert exc.value.exit_code == 2


def test_build_source_connector_llamaparse_without_env_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(_typer.Exit) as exc:
        _build_source_connector(
            tmp_path, pdf_backend="llamaparse", llama_api_key_env="LLAMA_CLOUD_API_KEY"
        )
    assert exc.value.exit_code == 2


def test_build_source_connector_llamaparse_mixed_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontos.cli.main import _build_source_connector
    from ontos.ingest import LlamaParsePdfConnector, MultiConnector, TextConnector

    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "llx-fake")
    (tmp_path / "notes.md").write_text("some notes")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4\n")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend="llamaparse", llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert isinstance(connector, MultiConnector)
    assert count == 2  # one text + one pdf
    kinds = {type(sc) for sc in connector.sub_connectors}
    assert TextConnector in kinds
    assert LlamaParsePdfConnector in kinds


def test_build_source_connector_unknown_backend_exits(tmp_path: Path) -> None:
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(_typer.Exit) as exc:
        _build_source_connector(
            tmp_path, pdf_backend="nonsense", llama_api_key_env="LLAMA_CLOUD_API_KEY"
        )
    assert exc.value.exit_code == 2


async def test_build_source_connector_pdf_dispatch_yields_unique_source_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: CLI → MultiConnector → LlamaParsePdfConnector.iter_documents().

    Covers the CLI-to-connector wiring with mocked LlamaParse, including
    the fix for the PR #40 blocker: two same-named PDFs in different
    subdirectories must emit distinct `source_id`s so provenance stays
    citable per file.
    """
    import sys
    import types

    # Install stub llama_cloud_services so the connector doesn't need the real dep.
    class _StubDocument:
        def __init__(self, text: str) -> None:
            self.text = text

    class _StubLlamaParse:
        def __init__(self, **kwargs: object) -> None:
            self._pages = [_StubDocument("stub markdown page")]

        async def aload_data(self, path: str) -> list[_StubDocument]:
            return list(self._pages)

    mod = types.ModuleType("llama_cloud_services")
    mod.LlamaParse = _StubLlamaParse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cloud_services", mod)
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "llx-fake")

    from ontos.cli.main import _build_source_connector

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "report.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "b" / "report.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "notes.md").write_text("some notes")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend="llamaparse", llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert count == 3  # 1 md + 2 pdfs

    docs = [d async for d in connector.iter_documents()]
    ids = sorted(d.source_id for d in docs)
    assert ids == ["a/report.pdf", "b/report.pdf", "notes.md"]
    # No source_id leaks the absolute tmp_path prefix.
    for d in docs:
        assert str(tmp_path) not in d.source_id


def test_build_source_connector_empty_dir_exits(tmp_path: Path) -> None:
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    with pytest.raises(_typer.Exit) as exc:
        _build_source_connector(tmp_path, pdf_backend=None, llama_api_key_env="LLAMA_CLOUD_API_KEY")
    assert exc.value.exit_code == 2


# --- #29: storage-path resolution + CLI persistence ---


def test_resolve_storage_path_cli_flag_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontos.cli.main import _resolve_storage_path

    monkeypatch.setenv("ONTOS_STORAGE_PATH", "/env/path/store.pkl")
    cli = str(tmp_path / "cli.pkl")
    assert _resolve_storage_path(cli) == Path(cli).resolve()


def test_resolve_storage_path_empty_cli_means_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.cli.main import _resolve_storage_path

    monkeypatch.setenv("ONTOS_STORAGE_PATH", "/env/path/store.pkl")
    assert _resolve_storage_path("") is None


def test_resolve_storage_path_env_used_when_cli_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontos.cli.main import _resolve_storage_path

    env_path = tmp_path / "env.pkl"
    monkeypatch.setenv("ONTOS_STORAGE_PATH", str(env_path))
    assert _resolve_storage_path(None) == env_path.resolve()


def test_resolve_storage_path_empty_env_means_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.cli.main import _resolve_storage_path

    monkeypatch.setenv("ONTOS_STORAGE_PATH", "")
    assert _resolve_storage_path(None) is None


def test_resolve_storage_path_default_used_when_nothing_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default is ~/.ontos/dev-store.pkl. Monkeypatch Path.home() so tests
    don't touch the real home directory."""
    from ontos.cli import main as cli_main

    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)
    monkeypatch.delenv("ONTOS_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Re-evaluate via the fresh home dir.
    resolved = cli_main._resolve_storage_path(None)
    assert resolved == tmp_path / ".ontos" / "dev-store.pkl"


def test_resolve_storage_path_no_default_for_non_networkx_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontos.cli.main import _resolve_storage_path

    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "neo4j")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _resolve_storage_path(None) is None


def test_build_cli_runtime_config_overrides_only_storage_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontos.cli.main import _build_cli_runtime_config

    monkeypatch.setenv("ONTOS_LISTEN_PORT", "9999")
    pkl = tmp_path / "x.pkl"
    cfg = _build_cli_runtime_config(pkl)
    assert cfg.storage_path == pkl
    assert cfg.listen_port == 9999  # other fields untouched


def test_warn_if_ephemeral_fires_for_networkx_with_no_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ontos.cli.main import _warn_if_ephemeral

    _warn_if_ephemeral(None, "networkx")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "in-memory" in captured.err
    assert "--storage-path" in captured.err


def test_warn_if_ephemeral_silent_for_persistent_networkx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ontos.cli.main import _warn_if_ephemeral

    _warn_if_ephemeral(tmp_path / "x.pkl", "networkx")
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err


def test_warn_if_ephemeral_silent_for_neo4j(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ontos.cli.main import _warn_if_ephemeral

    _warn_if_ephemeral(None, "neo4j")
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err


def test_ingest_help_documents_storage_path() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "--storage-path" in result.stdout


def test_query_help_documents_storage_path() -> None:
    result = runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert "--storage-path" in result.stdout


def test_serve_help_documents_storage_path() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--storage-path" in result.stdout


def test_no_args_prints_help() -> None:
    result = runner.invoke(app, [])
    # Typer's no_args_is_help sends the help output and exits with 2 (usage).
    assert "serve" in result.stdout or "ingest" in result.stdout
