"""Tests for shared LLM prompt construction."""

from pathlib import Path

from ontos.llm.prompts import planner_prompt
from ontos.ontology import load_ontology

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


def test_planner_prompt_explains_traversal_direction() -> None:
    ontology = load_ontology(STARTER_PATH)

    system, user = planner_prompt(
        "Who works at Acme Corp?",
        ontology,
    )

    prompt = f"{system}\n{user}"

    assert 'direction="out"' in prompt
    assert 'direction="in"' in prompt
    assert "subject side" in prompt
    assert "object side" in prompt
