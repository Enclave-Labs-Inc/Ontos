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


@pytest.fixture(autouse=True)
def _isolated_audit_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}",
    )
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-signing-key-not-for-prod")


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
    assert "--ollama-timeout" in result.stdout
    assert "--error-policy" in result.stdout
    assert "--pdf-backend" in result.stdout
    assert "--llama-api-key-env" in result.stdout


def test_query_help_documents_ollama_timeout() -> None:
    result = runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert "--ollama-timeout" in result.stdout


def test_ingest_threads_ollama_timeout_into_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#30: --ollama-timeout 180 must land as OllamaBackend(timeout_s=180.0)."""
    import ontos.extraction
    import ontos.llm

    captured_kwargs: dict[str, object] = {}

    class _CapturingLLM:
        def __init__(self, model: str, **kwargs: object) -> None:
            captured_kwargs["model"] = model
            captured_kwargs.update(kwargs)

    class _StubExtractor:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

        async def extract(self, inp: object) -> object:
            import types

            return types.SimpleNamespace(facts=[], entities=[], warnings=[])

    source_dir = tmp_path / "corpus"
    source_dir.mkdir()
    (source_dir / "doc.md").write_text("placeholder")
    pkl = tmp_path / "store.pkl"

    monkeypatch.setattr(ontos.llm, "OllamaBackend", _CapturingLLM)
    monkeypatch.setattr(ontos.extraction, "LlmExtractor", _StubExtractor)
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--source-dir",
            str(source_dir),
            "--ontology",
            str(STARTER_PATH),
            "--ollama-timeout",
            "180",
            "--storage-path",
            str(pkl),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured_kwargs["timeout_s"] == 180.0


def test_query_threads_ollama_timeout_into_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the ingest test for the query path."""
    import ontos.llm
    import ontos.planner
    from ontos.planner import Plan, SeedByEntity

    captured_kwargs: dict[str, object] = {}

    class _CapturingLLM:
        def __init__(self, model: str, **kwargs: object) -> None:
            captured_kwargs["model"] = model
            captured_kwargs.update(kwargs)

    class _StubPlanner:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

        async def plan(self, question: str, ontology: object) -> Plan:
            return Plan.build(
                seed=SeedByEntity(entity_id="x"),
                steps=[],
                ontology=ontology,  # type: ignore[arg-type]
            )

    monkeypatch.setattr(ontos.llm, "OllamaBackend", _CapturingLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _StubPlanner)

    result = runner.invoke(
        app,
        [
            "query",
            "anything",
            "--ontology",
            str(STARTER_PATH),
            "--ollama-timeout",
            "240.5",
            "--storage-path",
            "",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured_kwargs["timeout_s"] == 240.5


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


def test_build_source_connector_pypdf_single_pdf(tmp_path: Path) -> None:
    """#39: --pdf-backend pypdf dispatches to PypdfConnector with
    root=source_dir, no api key, no env lookup."""
    from ontos.cli.main import _build_source_connector
    from ontos.ingest import MultiConnector, PypdfConnector

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4\n")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend="pypdf", llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert isinstance(connector, MultiConnector)
    assert count == 1
    assert len(connector.sub_connectors) == 1
    assert isinstance(connector.sub_connectors[0], PypdfConnector)


def test_build_source_connector_pypdf_mixed_dir(tmp_path: Path) -> None:
    """Mixed .md + .pdf with --pdf-backend pypdf → both sub-connectors."""
    from ontos.cli.main import _build_source_connector
    from ontos.ingest import MultiConnector, PypdfConnector, TextConnector

    (tmp_path / "notes.md").write_text("some notes")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4\n")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend="pypdf", llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert isinstance(connector, MultiConnector)
    assert count == 2
    kinds = {type(sc) for sc in connector.sub_connectors}
    assert TextConnector in kinds
    assert PypdfConnector in kinds


def test_build_source_connector_pypdf_no_env_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sovereign path: LLAMA_CLOUD_API_KEY absence must not matter."""
    from ontos.cli.main import _build_source_connector

    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    connector, count = _build_source_connector(
        tmp_path, pdf_backend="pypdf", llama_api_key_env="LLAMA_CLOUD_API_KEY"
    )
    assert count == 1  # succeeds without the env var


def test_build_source_connector_unknown_backend_message_mentions_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error on unknown --pdf-backend must list both supported values."""
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(_typer.Exit):
        _build_source_connector(
            tmp_path, pdf_backend="nonsense", llama_api_key_env="LLAMA_CLOUD_API_KEY"
        )
    out = capsys.readouterr().out
    assert "pypdf" in out
    assert "llamaparse" in out


def test_build_source_connector_missing_backend_message_mentions_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error when PDFs present but --pdf-backend unset lists both choices."""
    import typer as _typer

    from ontos.cli.main import _build_source_connector

    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    with pytest.raises(_typer.Exit):
        _build_source_connector(tmp_path, pdf_backend=None, llama_api_key_env="LLAMA_CLOUD_API_KEY")
    out = capsys.readouterr().out
    assert "pypdf" in out
    assert "llamaparse" in out


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


def test_cli_ingest_persists_facts_through_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: invoke `ontos ingest --storage-path PATH` through typer's
    CliRunner with a stubbed extractor; verify the pickle lands + holds the
    expected fact. Locks in the #29 fix the review-comment flagged as
    untested — the `_run_pipeline` + close() wiring that is the actual fix.
    """
    from datetime import datetime

    import ontos.extraction
    import ontos.llm
    from ontos.runtime.models import Confidence, Entity, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    source_dir = tmp_path / "corpus"
    source_dir.mkdir()
    (source_dir / "notes.md").write_text("Alice works at Acme Corporation.")
    pkl = tmp_path / "store.pkl"

    class _StubLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _StubExtractor:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

        async def extract(self, inp: object) -> object:
            fact = Fact(
                subject_id="Alice",
                predicate="works_at",
                object_id="Acme",
                provenance=Provenance(
                    source_id="notes.md",
                    extractor_id="stub",
                    extractor_version="0",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
                t_valid=datetime(2026, 1, 1),
                ingested_at=datetime(2026, 1, 1),
            )
            entity = Entity(
                id="Alice",
                type="Person",
                canonical_name="Alice",
                provenance=fact.provenance,
            )
            import types

            return types.SimpleNamespace(facts=[fact], entities=[entity], warnings=[])

    monkeypatch.setattr(ontos.extraction, "LlmExtractor", _StubExtractor)
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    # Keep from_env from touching real envs:
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)
    monkeypatch.delenv("ONTOS_STORAGE_BACKEND", raising=False)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--source-dir",
            str(source_dir),
            "--ontology",
            str(STARTER_PATH),
            "--storage-path",
            str(pkl),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert pkl.exists(), "store file was not created by the ingest run"
    assert str(pkl) in result.stdout, "ingest did not report where facts were persisted"

    # Second process (same runner) reads back the facts.
    reader = NetworkxStore(path=pkl)

    import asyncio

    hits = asyncio.run(reader.facts_for_entity("Alice"))
    assert len(hits) == 1
    assert hits[0].predicate == "works_at"
    assert hits[0].object_id == "Acme"


def test_cli_query_does_not_rewrite_the_store_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only `ontos query` must not touch the pickle's mtime.

    Review-flagged correctness bug: without the dirty-flag gate, query
    rewrites the file on close() and can clobber a concurrent ingest
    with its stale snapshot. This test fails on main-pre-PR-review-fix.
    """
    import asyncio
    import time
    from datetime import datetime

    import ontos.llm
    import ontos.planner
    from ontos.planner import Plan, SeedByEntity
    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    pkl = tmp_path / "concurrent.pkl"

    # Prime the file with one fact.
    writer = NetworkxStore(path=pkl)
    asyncio.run(
        writer.add_fact(
            Fact(
                subject_id="Alice",
                predicate="works_at",
                object_id="Acme",
                provenance=Provenance(
                    source_id="s",
                    extractor_id="e",
                    extractor_version="0",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
                t_valid=datetime(2026, 1, 1),
                ingested_at=datetime(2026, 1, 1),
            )
        )
    )
    asyncio.run(writer.close())
    before_mtime = pkl.stat().st_mtime_ns
    before_bytes = pkl.read_bytes()

    class _StubLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _StubPlanner:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

        async def plan(self, question: str, ontology: object) -> Plan:
            return Plan.build(
                seed=SeedByEntity(entity_id="Alice"),
                steps=[],
                ontology=ontology,  # type: ignore[arg-type]
            )

    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _StubPlanner)

    time.sleep(0.01)  # ensure any accidental rewrite would bump mtime

    result = runner.invoke(
        app,
        [
            "query",
            "Who works at Acme?",
            "--ontology",
            str(STARTER_PATH),
            "--storage-path",
            str(pkl),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # File untouched.
    assert pkl.stat().st_mtime_ns == before_mtime, "query rewrote the store file"
    assert pkl.read_bytes() == before_bytes


def test_ingest_and_query_help_document_llm_backend() -> None:
    for command in ("ingest", "query"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--llm-backend" in result.stdout
        assert "--anthropic-model" in result.stdout
        assert "--anthropic-api-key-env" in result.stdout


def test_query_anthropic_without_api_key_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_TEST_MISSING_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "query",
            "anything",
            "--ontology",
            str(STARTER_PATH),
            "--llm-backend",
            "anthropic",
            "--anthropic-api-key-env",
            "ONTOS_TEST_MISSING_KEY",
            "--storage-path",
            "",
        ],
    )
    assert result.exit_code == 2
    assert "ONTOS_TEST_MISSING_KEY" in result.stderr


def test_query_unknown_llm_backend_exits() -> None:
    result = runner.invoke(
        app,
        [
            "query",
            "anything",
            "--ontology",
            str(STARTER_PATH),
            "--llm-backend",
            "gpt-something",
            "--storage-path",
            "",
        ],
    )
    assert result.exit_code == 2
    assert "unknown --llm-backend" in result.stderr


def test_query_threads_anthropic_model_and_key_into_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.llm
    import ontos.planner
    from ontos.planner import Plan, SeedByEntity

    captured_kwargs: dict[str, object] = {}

    class _CapturingLLM:
        id = "anthropic:test"

        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

    class _StubPlanner:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

        async def plan(self, question: str, ontology: object) -> Plan:
            return Plan.build(
                seed=SeedByEntity(entity_id="x"),
                steps=[],
                ontology=ontology,  # type: ignore[arg-type]
            )

    monkeypatch.setattr(ontos.llm, "AnthropicBackend", _CapturingLLM)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _StubPlanner)
    monkeypatch.setenv("ONTOS_TEST_ANTHROPIC_KEY", "sk-test")

    result = runner.invoke(
        app,
        [
            "query",
            "anything",
            "--ontology",
            str(STARTER_PATH),
            "--llm-backend",
            "anthropic",
            "--anthropic-model",
            "claude-sonnet-5",
            "--anthropic-api-key-env",
            "ONTOS_TEST_ANTHROPIC_KEY",
            "--storage-path",
            "",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured_kwargs == {"model": "claude-sonnet-5", "api_key": "sk-test"}


# --- #37: top-level storage URL resolution ---


def test_build_cli_runtime_config_storage_url_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.cli.main import _build_cli_runtime_config

    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.setenv("NEO4J_USER", "env-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-password")

    cfg = _build_cli_runtime_config(
        storage_path=None,
        storage_url="neo4j://cli-host:7687/cli-db",
        storage_password_env=None,
    )

    assert cfg.storage_backend == "neo4j"
    assert cfg.storage_path is None
    assert cfg.neo4j_uri == "neo4j://cli-host:7687"
    assert cfg.neo4j_user == "env-user"
    assert cfg.neo4j_password == "env-password"
    assert cfg.neo4j_database == "cli-db"


def test_build_cli_runtime_config_password_env_overrides_env_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.cli.main import _build_cli_runtime_config

    monkeypatch.setenv("NEO4J_PASSWORD", "ordinary-env-password")
    monkeypatch.setenv("MY_STORAGE_PASSWORD", "dedicated-secret")

    cfg = _build_cli_runtime_config(
        storage_path=None,
        storage_url="neo4j://alice@cli-host:7687/cli-db",
        storage_password_env="MY_STORAGE_PASSWORD",
    )

    assert cfg.neo4j_user == "alice"
    assert cfg.neo4j_password == "dedicated-secret"


def test_build_cli_runtime_config_password_env_must_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer as _typer

    from ontos.cli.main import _build_cli_runtime_config

    monkeypatch.delenv("MISSING_STORAGE_PASSWORD", raising=False)

    with pytest.raises(_typer.BadParameter, match="MISSING_STORAGE_PASSWORD"):
        _build_cli_runtime_config(
            storage_path=None,
            storage_url="neo4j://cli-host:7687/cli-db",
            storage_password_env="MISSING_STORAGE_PASSWORD",
        )


def test_build_cli_runtime_config_rejects_storage_and_storage_path_together(
    tmp_path: Path,
) -> None:
    import typer as _typer

    from ontos.cli.main import _build_cli_runtime_config

    with pytest.raises(_typer.BadParameter, match="--storage-path"):
        _build_cli_runtime_config(
            storage_path=tmp_path / "store.pkl",
            storage_url="networkx://",
            storage_password_env=None,
        )


def test_top_level_help_documents_storage_url_options() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--storage" in result.stdout
    assert "--storage-password-env" in result.stdout


def test_top_level_storage_option_is_accepted_before_subcommand() -> None:
    result = runner.invoke(
        app,
        [
            "--storage",
            "networkx://",
            "query",
            "--help",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "--ontology" in result.stdout


def test_query_threads_top_level_storage_options_into_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.cli.main as cli_main

    seen: dict[str, object] = {}

    def fake_build_cli_runtime_config(
        storage_path: Path | None,
        *,
        storage_url: str | None = None,
        storage_password_env: str | None = None,
    ) -> object:
        seen["storage_path"] = storage_path
        seen["storage_url"] = storage_url
        seen["storage_password_env"] = storage_password_env
        raise RuntimeError("storage-wiring-stop")

    monkeypatch.setattr(
        cli_main,
        "_build_cli_runtime_config",
        fake_build_cli_runtime_config,
    )

    result = runner.invoke(
        app,
        [
            "--storage",
            "neo4j://db.example:7687/knowledge",
            "--storage-password-env",
            "ONTOS_TEST_STORAGE_PASSWORD",
            "query",
            "anything",
            "--ontology",
            str(STARTER_PATH),
        ],
    )

    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "storage-wiring-stop"
    assert seen == {
        "storage_path": None,
        "storage_url": "neo4j://db.example:7687/knowledge",
        "storage_password_env": "ONTOS_TEST_STORAGE_PASSWORD",
    }


def test_ingest_threads_top_level_storage_options_into_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.cli.main as cli_main

    source_dir = tmp_path / "corpus"
    source_dir.mkdir()
    (source_dir / "doc.md").write_text("placeholder")

    seen: dict[str, object] = {}

    def fake_build_cli_runtime_config(
        storage_path: Path | None,
        *,
        storage_url: str | None = None,
        storage_password_env: str | None = None,
    ) -> object:
        seen["storage_path"] = storage_path
        seen["storage_url"] = storage_url
        seen["storage_password_env"] = storage_password_env
        raise RuntimeError("storage-wiring-stop")

    monkeypatch.setattr(
        cli_main,
        "_build_cli_runtime_config",
        fake_build_cli_runtime_config,
    )

    result = runner.invoke(
        app,
        [
            "--storage",
            "neo4j://db.example:7687/knowledge",
            "--storage-password-env",
            "ONTOS_TEST_STORAGE_PASSWORD",
            "ingest",
            "--source-dir",
            str(source_dir),
            "--ontology",
            str(STARTER_PATH),
        ],
    )

    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "storage-wiring-stop"
    assert seen == {
        "storage_path": None,
        "storage_url": "neo4j://db.example:7687/knowledge",
        "storage_password_env": "ONTOS_TEST_STORAGE_PASSWORD",
    }


def test_serve_threads_top_level_storage_into_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.runtime.server as runtime_server
    from ontos.runtime.config import RuntimeConfig

    seen: dict[str, object] = {}

    def fake_server_main(config: RuntimeConfig | None = None) -> None:
        seen["config"] = config

    monkeypatch.setattr(runtime_server, "main", fake_server_main)

    result = runner.invoke(
        app,
        [
            "--storage",
            "networkx://",
            "serve",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    cfg = seen["config"]
    assert isinstance(cfg, RuntimeConfig)
    assert cfg.storage_backend == "networkx"
    assert cfg.storage_path is None


def test_storage_url_password_is_not_exposed_in_cli_error() -> None:
    secret = "issue37-super-secret-password"

    result = runner.invoke(
        app,
        [
            "--storage",
            f"neo4j://alice:{secret}@db.example:not-a-port/knowledge",
            "serve",
        ],
    )

    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert "invalid port" in combined.lower()


def test_serve_snapshot_rejects_top_level_storage(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot.pkl"
    snapshot.write_bytes(b"not-loaded-because-conflict-is-checked-first")

    result = runner.invoke(
        app,
        [
            "--storage",
            "networkx://",
            "serve",
            "--snapshot",
            str(snapshot),
        ],
    )

    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert "--snapshot" in combined
    assert "--storage" in combined
    assert "mutually exclusive" in combined.lower()


def test_export_help_lists_supported_formats() -> None:
    result = runner.invoke(app, ["export", "--help"])

    assert result.exit_code == 0
    assert "neo4j" in result.stdout
    assert "graphml" in result.stdout
    assert "svg" in result.stdout
    assert "json-ld" in result.stdout


def test_export_graphml_writes_snapshot_file(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime
    from xml.etree import ElementTree as ET

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "graph.graphml"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 2, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert output_path.exists()

    root = ET.fromstring(output_path.read_text(encoding="utf-8"))
    assert root.tag.endswith("graphml")
    assert str(fact.id) in output_path.read_text(encoding="utf-8")


def test_export_json_ld_writes_snapshot_file(tmp_path: Path) -> None:
    import asyncio
    import json
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "graph.jsonld"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 2, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "json-ld",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert output_path.exists()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "@graph" in payload
    assert str(fact.id) in output_path.read_text(encoding="utf-8")


def test_export_svg_writes_snapshot_file(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime
    from xml.etree import ElementTree as ET

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "graph.svg"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 2, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "svg",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert output_path.exists()

    root = ET.fromstring(output_path.read_text(encoding="utf-8"))
    assert root.tag.endswith("svg")
    assert "person:alice" in output_path.read_text(encoding="utf-8")


def test_export_neo4j_writes_cypher_file(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "cypher.txt"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 2, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "neo4j",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert output_path.exists()

    cypher = output_path.read_text(encoding="utf-8")
    assert "MERGE" in cypher
    assert "person:alice" in cypher
    assert str(fact.id) in cypher


@pytest.mark.parametrize(
    "as_of",
    [
        "2026-03-01T00:00:00+00:00",
        # #60: offset-less values are interpreted as UTC instead of crashing
        # the comparison against timezone-aware stored timestamps.
        "2026-03-01",
        "2026-03-01T00:00:00",
    ],
)
def test_export_graphml_as_of_includes_historical_fact(tmp_path: Path, as_of: str) -> None:
    import asyncio
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "historical.graphml"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(
        writer.close_fact(
            fact.id,
            t_invalid=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--as-of",
            as_of,
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert str(fact.id) in output_path.read_text(encoding="utf-8")


def test_export_graphml_include_closed_includes_closed_fact(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "audit.graphml"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(
        writer.close_fact(
            fact.id,
            t_invalid=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--include-closed",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert str(fact.id) in output_path.read_text(encoding="utf-8")


def test_export_rejects_as_of_with_include_closed(tmp_path: Path) -> None:
    output_path = tmp_path / "graph.graphml"

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            "",
            "--output",
            str(output_path),
            "--as-of",
            "2026-03-01T00:00:00+00:00",
            "--include-closed",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "as-of" in output
    assert "include-closed" in output


def test_export_graphml_entity_hops_limits_neighborhood(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "neighborhood.graphml"

    writer = NetworkxStore(path=store_path)

    def fact(subject: str, predicate: str, object_: str) -> Fact:
        return Fact(
            subject_id=subject,
            predicate=predicate,
            object_id=object_,
            provenance=Provenance(
                source_id="doc:1",
                extractor_id="extractor:test",
                extractor_version="1",
                ontology_id="ontos.starter",
                ontology_version="0.4",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=datetime(2026, 1, 1, tzinfo=UTC),
            ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    asyncio.run(writer.add_fact(fact("entity:a", "rel", "entity:b")))
    asyncio.run(writer.add_fact(fact("entity:b", "rel", "entity:c")))
    asyncio.run(writer.add_fact(fact("entity:c", "rel", "entity:d")))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--entity",
            "entity:a",
            "--hops",
            "2",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert "entity:a" in exported
    assert "entity:b" in exported
    assert "entity:c" in exported
    assert "entity:d" not in exported


def test_export_rejects_hops_without_entity(tmp_path: Path) -> None:
    output_path = tmp_path / "graph.graphml"

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            "",
            "--output",
            str(output_path),
            "--hops",
            "3",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "hops" in output
    assert "entity" in output


def test_export_entity_hops_includes_merge_lineage(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    from ontos.resolver.base import MergeRecord
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "merge-neighborhood.graphml"

    writer = NetworkxStore(path=store_path)
    asyncio.run(
        writer.record_merge(
            MergeRecord(
                canonical_id="company:acme-1",
                merged_ids=["company:acme-2"],
                resolver_id="exact-match",
                resolver_version="1.0",
                resolved_at=datetime(2026, 1, 3, tzinfo=UTC),
                reason="same registration number",
            )
        )
    )
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--entity",
            "company:acme-2",
            "--hops",
            "1",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert "company:acme-2" in exported
    assert "company:acme-1" in exported
    assert "MERGED_WITH" in exported


def test_export_neo4j_help_documents_push_options() -> None:
    result = runner.invoke(app, ["export", "neo4j", "--help"])

    assert result.exit_code == 0
    assert "--push" in result.stdout
    assert "--user" in result.stdout
    assert "--password" in result.stdout
    assert "--database" in result.stdout


def test_export_neo4j_push_uses_requested_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neo4j

    import ontos.exporters.cypher

    captured: dict[str, object] = {}

    class _FakeDriver:
        async def close(self) -> None:
            captured["closed"] = True

    def _fake_driver(
        uri: str,
        *,
        auth: tuple[str, str],
    ) -> _FakeDriver:
        captured["uri"] = uri
        captured["auth"] = auth
        return _FakeDriver()

    async def _fake_push(
        scan: object,
        driver: object,
        *,
        database: str,
    ) -> None:
        captured["scan"] = scan
        captured["driver"] = driver
        captured["database"] = database

    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", _fake_driver)
    monkeypatch.setattr(ontos.exporters.cypher, "push_neo4j", _fake_push)

    result = runner.invoke(
        app,
        [
            "export",
            "neo4j",
            "--storage-path",
            "",
            "--push",
            "bolt://viz.example:7687",
            "--user",
            "viz-user",
            "--password",
            "secret",
            "--database",
            "ontos-viz",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["uri"] == "bolt://viz.example:7687"
    assert captured["auth"] == ("viz-user", "secret")
    assert captured["database"] == "ontos-viz"
    assert captured["closed"] is True


def test_export_neo4j_push_rejects_operational_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", "bolt://operational.example:7687")
    monkeypatch.setenv("NEO4J_DATABASE", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "operational-secret")

    result = runner.invoke(
        app,
        [
            "export",
            "neo4j",
            "--push",
            "bolt://operational.example:7687",
            "--user",
            "viz-user",
            "--password",
            "viz-secret",
            "--database",
            "neo4j",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "operational" in output
    assert "separate" in output


def test_export_neo4j_push_rejects_operational_database_from_storage_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#62: the guard must see an operational store configured via --storage,
    and a different scheme / implicit port must not slip past it."""
    monkeypatch.setenv("ONTOS_TEST_NEO4J_PASSWORD", "operational-secret")

    result = runner.invoke(
        app,
        [
            "--storage",
            "neo4j://ops@operational.example/knowledge",
            "--storage-password-env",
            "ONTOS_TEST_NEO4J_PASSWORD",
            "export",
            "neo4j",
            "--push",
            "bolt://operational.example:7687",
            "--password",
            "viz-secret",
            "--database",
            "knowledge",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "operational" in output
    assert "operational-secret" not in output


def test_export_honors_global_storage_override(tmp_path: Path) -> None:
    """#62: `ontos --storage <url> export …` must read the --storage store,
    not silently fall back to the default dev store."""
    import asyncio
    from datetime import UTC, datetime

    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "override.graphml"

    writer = NetworkxStore(path=store_path)
    fact = Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc:1",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    asyncio.run(writer.add_fact(fact))
    asyncio.run(writer.close())

    result = runner.invoke(
        app,
        [
            "--storage",
            f"networkx://{store_path}",
            "export",
            "graphml",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert str(fact.id) in output_path.read_text(encoding="utf-8")


def test_export_rejects_storage_with_storage_path(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--storage",
            "networkx://",
            "export",
            "graphml",
            "--storage-path",
            str(tmp_path / "store.pkl"),
            "--output",
            str(tmp_path / "out.graphml"),
        ],
    )

    assert result.exit_code == 2
    assert "cannot be used together" in result.stdout + result.stderr


def test_export_neo4j_push_requires_password_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.cli.main

    def _unexpected_scan(*args: object, **kwargs: object) -> object:
        raise AssertionError("source graph must not be scanned before push validation")

    monkeypatch.setattr(ontos.cli.main, "_scan_for_export", _unexpected_scan)

    result = runner.invoke(
        app,
        [
            "export",
            "neo4j",
            "--push",
            "bolt://viz.example:7687",
            "--database",
            "ontos-viz",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "password" in output


def test_export_authorized_subject_can_see_allowed_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from datetime import UTC, datetime

    import ontos.runtime.server
    from ontos.authz import VIEW, InMemoryAuthz
    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "authorized.graphml"

    writer = NetworkxStore(path=store_path)

    def fact(
        subject: str,
        predicate: str,
        object_: str,
        *,
        acl_ref: str | None = None,
    ) -> Fact:
        when = datetime(2026, 1, 1, tzinfo=UTC)
        return Fact(
            subject_id=subject,
            predicate=predicate,
            object_id=object_,
            provenance=Provenance(
                source_id="doc:1",
                extractor_id="extractor:test",
                extractor_version="1",
                ontology_id="ontos.starter",
                ontology_version="0.4",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=when,
            ingested_at=when,
            acl_ref=acl_ref,
        )

    public_fact = fact(
        "company:acme",
        "has_public_profile",
        "profile:public",
    )
    finance_fact = fact(
        "company:acme",
        "has_finance_record",
        "record:finance",
        acl_ref="acl:finance",
    )

    asyncio.run(writer.add_fact(public_fact))
    asyncio.run(writer.add_fact(finance_fact))
    asyncio.run(writer.close())

    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:finance")

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        lambda _cfg: authz,
    )

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--acting-on-behalf-of",
            "user:alice",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert str(public_fact.id) in exported
    assert str(finance_fact.id) in exported


def test_export_unauthorized_subject_gets_public_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from datetime import UTC, datetime

    import ontos.runtime.server
    from ontos.authz import InMemoryAuthz
    from ontos.runtime.models import Confidence, Fact, Provenance
    from ontos.storage.networkx_store import NetworkxStore

    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "unauthorized.graphml"

    writer = NetworkxStore(path=store_path)

    def fact(
        subject: str,
        predicate: str,
        object_: str,
        *,
        acl_ref: str | None = None,
    ) -> Fact:
        when = datetime(2026, 1, 1, tzinfo=UTC)
        return Fact(
            subject_id=subject,
            predicate=predicate,
            object_id=object_,
            provenance=Provenance(
                source_id="doc:1",
                extractor_id="extractor:test",
                extractor_version="1",
                ontology_id="ontos.starter",
                ontology_version="0.4",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=when,
            ingested_at=when,
            acl_ref=acl_ref,
        )

    public_fact = fact(
        "company:acme",
        "has_public_profile",
        "profile:public",
    )
    finance_fact = fact(
        "company:acme",
        "has_finance_record",
        "record:finance",
        acl_ref="acl:finance",
    )

    asyncio.run(writer.add_fact(public_fact))
    asyncio.run(writer.add_fact(finance_fact))
    asyncio.run(writer.close())

    authz = InMemoryAuthz()

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        lambda _cfg: authz,
    )

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--acting-on-behalf-of",
            "user:bob",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert str(public_fact.id) in exported
    assert str(finance_fact.id) not in exported
