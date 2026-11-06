"""CLI opt-in contract for Issue #66 M2b.

Introspects the parsed command instead of rendered help: under
GITHUB_ACTIONS Rich ignores COLUMNS and truncates long option names, so
text assertions on `--help` output were fragile (#90).
"""

import typer
from typer.testing import CliRunner

from ontos.cli.main import app

runner = CliRunner()


def _suggestions_list_options() -> set[str]:
    root = typer.main.get_command(app)
    ontology = root.commands["ontology"]  # type: ignore[attr-defined]
    command = ontology.commands["suggestions"].commands["list"]
    return {opt for param in command.params for opt in param.opts}


def test_m2b_llm_model_option_is_available() -> None:
    assert {
        "--llm-model",
        "--ollama-timeout",
        "--acting-on-behalf-of",
        "--ontology",
    } <= _suggestions_list_options()
    assert runner.invoke(app, ["ontology", "suggestions", "list", "--help"]).exit_code == 0


def test_m2b_requires_model_value_when_selected() -> None:
    result = runner.invoke(app, ["ontology", "suggestions", "list", "--llm-model"])

    # Click's usage-error exit code, whatever the rendering.
    assert result.exit_code == 2
