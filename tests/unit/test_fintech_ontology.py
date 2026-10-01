"""M7.a — fintech vertical ontology regression test.

The fintech ontology at `docs/ontology/examples/fintech.yaml` is one
of the two vertical ontologies shipped with 0.1.x. This test locks the
load-bearing shape so a well-meaning edit can't quietly break the
graded questions in `tests/verticals/test_fintech_graded.py` (landing
in M7.c) or the sample-corpus ingestion in M7.e.

The test asserts the *shape and coverage* required for those follow-ups
— not every entity/relation, so this doesn't turn into a busywork test
every time someone adds a property.
"""

from __future__ import annotations

from pathlib import Path

from ontos.ontology import Ontology, load_ontology

FINTECH_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "fintech.yaml"
)


def test_fintech_ontology_loads() -> None:
    ont = load_ontology(FINTECH_PATH)
    assert isinstance(ont, Ontology)


def test_fintech_ontology_has_minimum_coverage() -> None:
    # A vertical ontology carries its own weight only when it's
    # actually broad enough to support real questions. If someone
    # shrinks it below this floor, they should update this floor
    # deliberately.
    ont = load_ontology(FINTECH_PATH)
    assert len(ont.entity_types) >= 15
    assert len(ont.relation_types) >= 20
    assert len(ont.patterns) >= 20


def test_fintech_ontology_covers_core_10k_entities() -> None:
    ont = load_ontology(FINTECH_PATH)
    for label in (
        "Person",
        "Company",
        "Auditor",
        "Regulator",
        "Filing",
        "FilingSection",
        "Risk",
        "Regulation",
        "BoardCommittee",
        "BusinessSegment",
        "MaterialEvent",
    ):
        assert ont.entity_type(label) is not None, f"missing entity type {label!r}"


def test_fintech_ontology_covers_sr_11_7_model_risk() -> None:
    # SR 11-7 is a defining regulatory vocabulary for regulated
    # financial-services buyers. If Model / ModelControl vanish, the
    # SR 11-7 graded questions in M7.c will silently unmatch.
    ont = load_ontology(FINTECH_PATH)
    assert ont.entity_type("Model") is not None
    assert ont.entity_type("ModelControl") is not None
    assert ont.relation_type("operates_model") is not None
    assert ont.relation_type("governed_by") is not None
    assert ont.is_valid_pattern("Company", "operates_model", "Model")
    assert ont.is_valid_pattern("Model", "governed_by", "ModelControl")


def test_fintech_ontology_supports_filing_provenance_chain() -> None:
    # The most important shape a compliance graded question relies on:
    # Company --filed-> Filing --contains_section-> FilingSection
    # --discloses_risk-> Risk. All four hops must be valid patterns
    # or multi-hop retrieval falls back to the vector store and the
    # provenance chain breaks.
    ont = load_ontology(FINTECH_PATH)
    assert ont.is_valid_pattern("Company", "filed", "Filing")
    assert ont.is_valid_pattern("Filing", "contains_section", "FilingSection")
    assert ont.is_valid_pattern("FilingSection", "discloses_risk", "Risk")


def test_fintech_ontology_relation_cardinalities_are_realistic() -> None:
    # Cardinality misalignment is CypherBench failure mode #3
    # ("reversed direction / pattern misalignment"), so pin the
    # ones a bad edit would most easily break.
    ont = load_ontology(FINTECH_PATH)

    filed = ont.relation_type("filed")
    assert filed is not None
    assert filed.cardinality == "one_to_many"  # one company → many filings

    audited_by = ont.relation_type("audited_by")
    assert audited_by is not None
    assert audited_by.cardinality == "many_to_one"  # one primary auditor at a time

    subsidiary_of = ont.relation_type("subsidiary_of")
    assert subsidiary_of is not None
    assert subsidiary_of.cardinality == "many_to_one"

    operates_model = ont.relation_type("operates_model")
    assert operates_model is not None
    assert operates_model.cardinality == "one_to_many"


def test_fintech_ontology_key_properties_present() -> None:
    # cik + ticker on Company, form_type + accession_number + fiscal_year
    # on Filing, and topic on Risk are the fields the graded questions
    # will filter on. If any go missing the questions can't be answered.
    ont = load_ontology(FINTECH_PATH)

    company = ont.entity_type("Company")
    assert company is not None
    company_props = {p.name for p in company.properties}
    assert {"legal_name", "cik", "ticker"} <= company_props

    filing = ont.entity_type("Filing")
    assert filing is not None
    filing_props = {p.name for p in filing.properties}
    assert {"form_type", "accession_number", "fiscal_year"} <= filing_props

    risk = ont.entity_type("Risk")
    assert risk is not None
    risk_props = {p.name for p in risk.properties}
    assert {"topic", "geography"} <= risk_props
