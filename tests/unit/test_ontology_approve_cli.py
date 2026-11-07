"""M3-2 approval CLI registration and input safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ontos.cli import app

runner = CliRunner()
STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _args(tmp_path: Path, change_file: Path) -> list[str]:
    return [
        "ontology",
        "approve",
        "--acting-on-behalf-of",
        "user:operator",
        "--ontology",
        str(STARTER),
        "--base-version",
        "0.1",
        "--new-version",
        "0.2",
        "--change-file",
        str(change_file),
        "--ontology-output",
        str(tmp_path / "ontology-0.2.yaml"),
        "--migration-output",
        str(tmp_path / "migration-0.2.yaml"),
        "--storage-path",
        str(tmp_path / "store.pkl"),
    ]


def test_approve_command_registered_and_options_present() -> None:
    root = typer.main.get_command(app)
    ontology = root.commands["ontology"]  # type: ignore[attr-defined]
    command = ontology.commands["approve"]
    options = {option for param in command.params for option in param.opts}

    assert {
        "--acting-on-behalf-of",
        "--ontology",
        "--base-version",
        "--new-version",
        "--change-file",
        "--ontology-output",
        "--migration-output",
        "--acl-ref",
        "--storage-path",
    } <= options
    assert runner.invoke(app, ["ontology", "approve", "--help"]).exit_code == 0


def test_approve_requires_explicit_arguments() -> None:
    assert runner.invoke(app, ["ontology", "approve"]).exit_code != 0


def test_approve_rejects_operator_supplied_approval_id(
    tmp_path: Path,
) -> None:
    change = tmp_path / "change.yaml"
    change.write_text(
        "action: add_pattern\n"
        "subject_type: Person\n"
        "predicate: acquired\n"
        "object_type: Company\n"
        "approval_query_id: 00000000-0000-0000-0000-000000000001\n"
    )
    result = runner.invoke(app, _args(tmp_path, change))

    assert result.exit_code != 0
    assert "Invalid approval change file" in result.output
    assert not (tmp_path / "ontology-0.2.yaml").exists()


def test_approve_requires_persistent_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = tmp_path / "change.yaml"
    change.write_text(
        "action: add_pattern\nsubject_type: Person\npredicate: acquired\nobject_type: Company\n"
    )
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", "")
    result = runner.invoke(app, _args(tmp_path, change))

    assert result.exit_code != 0
    assert "Persistent audit database is required" in result.output
    assert not (tmp_path / "migration-0.2.yaml").exists()
