"""M5.c — CLI tests via typer's CliRunner. Does not hit real Ollama / Postgres."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ontos.cli import app

# Force a wide "terminal" so Typer/Rich doesn't wrap option names.
# On CI (narrow TTY) --source-dir would render as --source-\ndir and
# the naive substring check would miss it. Env goes through to Click.
runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

STARTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "ontology" / "examples" / "starter.yaml"
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
    from ontos.cli.main import _read_source_dir

    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.md").write_text("bravo")
    (tmp_path / "c.pdf").write_text("charlie")  # should be skipped
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.md").write_text("delta")

    docs = _read_source_dir(tmp_path)
    assert set(docs.keys()) == {"a.txt", "b.md", "sub/d.md"}
    assert "c.pdf" not in docs
    assert docs["a.txt"] == "alpha"
    assert docs["sub/d.md"] == "delta"


def test_no_args_prints_help() -> None:
    result = runner.invoke(app, [])
    # Typer's no_args_is_help sends the help output and exits with 2 (usage).
    assert "serve" in result.stdout or "ingest" in result.stdout
