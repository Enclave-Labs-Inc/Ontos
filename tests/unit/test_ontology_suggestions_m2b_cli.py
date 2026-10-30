"""CLI opt-in contract for Issue #66 M2b."""

from typer.testing import CliRunner

from ontos.cli.main import app

runner = CliRunner()


def test_m2b_llm_model_option_is_available() -> None:
    result = runner.invoke(
        app,
        ["ontology", "suggestions", "list", "--help"],
    )

    assert result.exit_code == 0
    assert "--llm-model" in result.output
    assert "--acting-on-behalf-of" in result.output
    assert "--ontology" in result.output


def test_m2b_requires_model_value_when_selected() -> None:
    result = runner.invoke(
        app,
        ["ontology", "suggestions", "list", "--llm-model"],
    )

    assert result.exit_code != 0
    assert "requires an argument" in result.output.lower()
