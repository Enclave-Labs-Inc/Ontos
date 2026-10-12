from pathlib import Path

from ontos.llm.prompts import extraction_prompt
from ontos.ontology import load_ontology

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


def test_extraction_prompt_includes_relation_descriptions() -> None:
    ontology = load_ontology(STARTER_PATH)

    _system, user = extraction_prompt(
        "Beta Systems is a subsidiary of Acme Corp.",
        ontology,
    )

    assert "subsidiary_of [many_to_one]" in user
    assert (
        "The subject company is a subsidiary of the object company; "
        "the object company is the parent." in user
    )


def test_extraction_prompt_explicitly_preserves_relation_direction() -> None:
    ontology = load_ontology(STARTER_PATH)

    system, _user = extraction_prompt(
        "Beta Systems is a subsidiary of Acme Corp.",
        ontology,
    )

    assert "Preserve relation direction" in system
    assert "subject role" in system
    assert "object role" in system


def test_extraction_prompt_requires_complete_document_coverage() -> None:
    ontology = load_ontology(STARTER_PATH)

    system, _user = extraction_prompt(
        "Alice works at Acme. David works at Beta.",
        ontology,
    )

    assert "Process the entire input text" in system
    assert "Do not stop after finding a few valid triples" in system
    assert "Extract each distinct" in system
    assert "supported relationship once" in system
