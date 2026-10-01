"""M7.b — pharma vertical ontology regression test.

Same shape as `test_fintech_ontology.py`: locks the load-bearing
coverage needed for the pharma graded questions and CLI vertical
loader without turning into a churn-magnet on every property tweak.
"""

from __future__ import annotations

from pathlib import Path

from ontos.ontology import Ontology, load_ontology

PHARMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "pharma.yaml"


def test_pharma_ontology_loads() -> None:
    ont = load_ontology(PHARMA_PATH)
    assert isinstance(ont, Ontology)


def test_pharma_ontology_has_minimum_coverage() -> None:
    ont = load_ontology(PHARMA_PATH)
    assert len(ont.entity_types) >= 15
    assert len(ont.relation_types) >= 20
    assert len(ont.patterns) >= 20


def test_pharma_ontology_covers_clinical_and_regulatory_entities() -> None:
    ont = load_ontology(PHARMA_PATH)
    for label in (
        "Person",
        "Company",
        "CRO",
        "Regulator",
        "Regulation",
        "Drug",
        "MoleculeTarget",
        "Indication",
        "ClinicalTrial",
        "TrialSite",
        "AdverseEvent",
        "RegulatorySubmission",
        "ManufacturingSite",
        "QualityControl",
        "InspectionObservation",
    ):
        assert ont.entity_type(label) is not None, f"missing entity type {label!r}"


def test_pharma_ontology_supports_trial_provenance_chain() -> None:
    # A defining compliance question — "which trials evaluate this
    # drug, run by this CRO, at these sites, reporting these adverse
    # events?" — needs every hop below to be a valid pattern.
    ont = load_ontology(PHARMA_PATH)
    assert ont.is_valid_pattern("Company", "sponsors_trial", "ClinicalTrial")
    assert ont.is_valid_pattern("ClinicalTrial", "conducted_by", "CRO")
    assert ont.is_valid_pattern("ClinicalTrial", "evaluates", "Drug")
    assert ont.is_valid_pattern("ClinicalTrial", "enrolls_at", "TrialSite")
    assert ont.is_valid_pattern("ClinicalTrial", "reports_event", "AdverseEvent")


def test_pharma_ontology_supports_gxp_manufacturing_chain() -> None:
    # GxP audit-readiness question — "which manufacturing sites make
    # this drug, who operates them, when were they last inspected, and
    # what observations were raised?"
    ont = load_ontology(PHARMA_PATH)
    assert ont.is_valid_pattern("Drug", "manufactured_at", "ManufacturingSite")
    assert ont.is_valid_pattern("ManufacturingSite", "operated_by", "Company")
    assert ont.is_valid_pattern("ManufacturingSite", "inspected_by", "Regulator")
    assert ont.is_valid_pattern("ManufacturingSite", "has_control", "QualityControl")
    assert ont.is_valid_pattern("InspectionObservation", "noted_at", "ManufacturingSite")


def test_pharma_ontology_supports_regulatory_submission_chain() -> None:
    ont = load_ontology(PHARMA_PATH)
    assert ont.is_valid_pattern("Company", "submitted", "RegulatorySubmission")
    assert ont.is_valid_pattern("RegulatorySubmission", "reviewed_by", "Regulator")
    assert ont.is_valid_pattern("RegulatorySubmission", "covers_drug", "Drug")


def test_pharma_ontology_relation_cardinalities_are_realistic() -> None:
    ont = load_ontology(PHARMA_PATH)

    sponsors_trial = ont.relation_type("sponsors_trial")
    assert sponsors_trial is not None
    assert sponsors_trial.cardinality == "one_to_many"

    reviewed_by = ont.relation_type("reviewed_by")
    assert reviewed_by is not None
    # A submission goes to exactly one regulator; that regulator
    # reviews many submissions.
    assert reviewed_by.cardinality == "many_to_one"

    has_control = ont.relation_type("has_control")
    assert has_control is not None
    assert has_control.cardinality == "one_to_many"


def test_pharma_ontology_key_properties_present() -> None:
    ont = load_ontology(PHARMA_PATH)

    trial = ont.entity_type("ClinicalTrial")
    assert trial is not None
    trial_props = {p.name for p in trial.properties}
    # nct_id is the load-bearing cross-source identifier — mandatory.
    assert "nct_id" in trial_props
    assert {"phase", "status"} <= trial_props

    drug = ont.entity_type("Drug")
    assert drug is not None
    drug_props = {p.name for p in drug.properties}
    assert {"inn", "code", "modality", "approval_status"} <= drug_props

    submission = ont.entity_type("RegulatorySubmission")
    assert submission is not None
    submission_props = {p.name for p in submission.properties}
    assert {"submission_type", "submitted_at", "decision"} <= submission_props

    site = ont.entity_type("ManufacturingSite")
    assert site is not None
    site_props = {p.name for p in site.properties}
    # FDA establishment ID is the cross-source join key on the manufacturing side.
    assert "fda_establishment_id" in site_props
