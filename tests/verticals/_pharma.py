"""20 graded questions for the pharma vertical.

Same shape as `_fintech.py`. Difficulty distribution: single-hop (5),
multi-hop (7), cross-source (4), temporal (2), provenance (2).
"""

from __future__ import annotations

from tests.verticals._schema import GradedQuestion, Hop

PHARMA_QUESTIONS: list[GradedQuestion] = [
    # ------------------------------------------------------------------
    # Single-hop (5)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="pharma-q01",
        question="Who sponsors clinical trial NCT9999001?",
        difficulty="single_hop",
        hops=[
            Hop(subject_type="Company", predicate="sponsors_trial", object_type="ClinicalTrial")
        ],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["Orion Therapeutics", "NCT9999001"],
    ),
    GradedQuestion(
        id="pharma-q02",
        question="What is the INN and modality of ORT-451?",
        difficulty="single_hop",
        hops=[],  # property lookup on a single Drug node — no relation traversal
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["meraltacept", "monoclonal antibody"],
    ),
    GradedQuestion(
        id="pharma-q03",
        question="Which regulator reviewed NDA-224567?",
        difficulty="single_hop",
        hops=[
            Hop(
                subject_type="RegulatorySubmission",
                predicate="reviewed_by",
                object_type="Regulator",
            )
        ],
        expected_source_docs=["halcyon-hb207-nda-summary.txt"],
        expected_entities=["Food and Drug Administration", "CDER"],
    ),
    GradedQuestion(
        id="pharma-q04",
        question="What molecular target does ORT-451 act on?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Drug", predicate="targets", object_type="MoleculeTarget")],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["PD-L1"],
    ),
    GradedQuestion(
        id="pharma-q05",
        question="Which indication is trial NCT9999001 designed to treat?",
        difficulty="single_hop",
        hops=[
            Hop(
                subject_type="ClinicalTrial",
                predicate="treats_indication",
                object_type="Indication",
            )
        ],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["Metastatic Melanoma"],
    ),
    # ------------------------------------------------------------------
    # Multi-hop (7)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="pharma-q06",
        question="Which trial sites enroll patients in the ORT-451 Phase 3 trial?",
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="ClinicalTrial", predicate="enrolls_at", object_type="TrialSite"),
        ],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=[
            "Massachusetts General Hospital",
            "Mayo Clinic Rochester",
            "Charité",
        ],
    ),
    GradedQuestion(
        id="pharma-q07",
        question="Who is the coordinating principal investigator on trial NCT9999001?",
        difficulty="multi_hop",
        hops=[Hop(subject_type="Person", predicate="leads_trial", object_type="ClinicalTrial")],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["Elena Marquez", "Massachusetts General Hospital"],
    ),
    GradedQuestion(
        id="pharma-q08",
        question="Which manufacturing sites are named on the XYLEVA (halomectinib) drug label?",
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Drug", predicate="manufactured_at", object_type="ManufacturingSite"),
        ],
        expected_source_docs=["hb207-fda-label.txt"],
        expected_entities=[
            "Halcyon Bio Corp",
            "Andover",
            "Meridian Biopharm",
            "Bedford",
        ],
    ),
    GradedQuestion(
        id="pharma-q09",
        question="Which CRO conducts the ORT-451 Phase 3 trial?",
        difficulty="multi_hop",
        hops=[Hop(subject_type="ClinicalTrial", predicate="conducted_by", object_type="CRO")],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["Bluewave Clinical Research"],
    ),
    GradedQuestion(
        id="pharma-q10",
        question=(
            "What cGMP regulations does the Meridian Biopharm Bedford site's "
            "quality-system implement?"
        ),
        difficulty="multi_hop",
        hops=[
            Hop(
                subject_type="ManufacturingSite",
                predicate="has_control",
                object_type="QualityControl",
            ),
            Hop(
                subject_type="QualityControl",
                predicate="implements_regulation",
                object_type="Regulation",
            ),
        ],
        expected_source_docs=["meridian-biopharm-form483.txt", "hb207-fda-label.txt"],
        expected_entities=["21 CFR", "211", "cGMP"],
    ),
    GradedQuestion(
        id="pharma-q11",
        question=(
            "Which serious adverse events reported in trial NCT9999000 were "
            "attributed to ORT-451?"
        ),
        difficulty="multi_hop",
        hops=[
            Hop(
                subject_type="ClinicalTrial",
                predicate="reports_event",
                object_type="AdverseEvent",
            ),
            Hop(subject_type="AdverseEvent", predicate="caused_by", object_type="Drug"),
        ],
        expected_source_docs=["ort451-phase2-sae-summary.txt"],
        expected_entities=[
            "Immune-mediated colitis",
            "Immune-mediated pneumonitis",
            "Ventricular arrhythmia",
            "ORT-451",
        ],
    ),
    GradedQuestion(
        id="pharma-q12",
        question="What Grade-3 adverse events were reported in the Phase 2 ORT-451 trial?",
        difficulty="multi_hop",
        hops=[
            Hop(
                subject_type="ClinicalTrial",
                predicate="reports_event",
                object_type="AdverseEvent",
            ),
        ],
        expected_source_docs=["ort451-phase2-sae-summary.txt"],
        expected_entities=["Grade: 3", "Immune-mediated colitis", "Ventricular arrhythmia"],
    ),
    # ------------------------------------------------------------------
    # Cross-source (4)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="pharma-q13",
        question="Which drug appears in both the FDA-approved label and an NDA submission summary?",
        difficulty="cross_source",
        hops=[
            Hop(
                subject_type="RegulatorySubmission",
                predicate="covers_drug",
                object_type="Drug",
            )
        ],
        expected_source_docs=["hb207-fda-label.txt", "halcyon-hb207-nda-summary.txt"],
        expected_entities=["halomectinib", "XYLEVA", "HB-207"],
    ),
    GradedQuestion(
        id="pharma-q14",
        question=(
            "Which manufacturing site named on an FDA drug label was also cited "
            "in an FDA Form 483?"
        ),
        difficulty="cross_source",
        hops=[
            Hop(
                subject_type="Drug",
                predicate="manufactured_at",
                object_type="ManufacturingSite",
            ),
            Hop(
                subject_type="InspectionObservation",
                predicate="noted_at",
                object_type="ManufacturingSite",
            ),
        ],
        expected_source_docs=["hb207-fda-label.txt", "meridian-biopharm-form483.txt"],
        expected_entities=["Meridian Biopharm", "Bedford", "3009654321"],
    ),
    GradedQuestion(
        id="pharma-q15",
        question=(
            "Which sponsor appears in both the Phase 3 trial record and the "
            "Phase 2 SAE summary for ORT-451?"
        ),
        difficulty="cross_source",
        hops=[
            Hop(subject_type="Company", predicate="sponsors_trial", object_type="ClinicalTrial")
        ],
        expected_source_docs=[
            "ort451-nct9999001-record.txt",
            "ort451-phase2-sae-summary.txt",
        ],
        expected_entities=["Orion Therapeutics"],
    ),
    GradedQuestion(
        id="pharma-q16",
        question=(
            "Which FDA establishment identifier appears in both a Form 483 and "
            "an NDA submission summary?"
        ),
        difficulty="cross_source",
        hops=[
            Hop(
                subject_type="Drug",
                predicate="manufactured_at",
                object_type="ManufacturingSite",
            )
        ],
        expected_source_docs=[
            "meridian-biopharm-form483.txt",
            "halcyon-hb207-nda-summary.txt",
        ],
        expected_entities=["3009654321", "Meridian Biopharm", "Bedford"],
    ),
    # ------------------------------------------------------------------
    # Temporal (2)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="pharma-q17",
        question=(
            "Which sNDA supplements to NDA-224567 had been approved as of "
            "September 1, 2024?"
        ),
        difficulty="temporal",
        hops=[
            Hop(
                subject_type="Company",
                predicate="submitted",
                object_type="RegulatorySubmission",
            ),
            Hop(
                subject_type="RegulatorySubmission",
                predicate="reviewed_by",
                object_type="Regulator",
            ),
        ],
        as_of="2024-09-01",
        expected_source_docs=["halcyon-hb207-nda-summary.txt"],
        expected_entities=["sNDA-224567/S-001", "2024-03-15"],
        notes=(
            "S-002 was approved 2024-09-12, after the Sept 1 cutoff; only "
            "S-001 (approved 2024-03-15) had been approved as of the query date."
        ),
    ),
    GradedQuestion(
        id="pharma-q18",
        question=(
            "As of December 31, 2024, which sNDA supplements to NDA-224567 "
            "remained under FDA review?"
        ),
        difficulty="temporal",
        hops=[
            Hop(
                subject_type="Company",
                predicate="submitted",
                object_type="RegulatorySubmission",
            ),
        ],
        as_of="2024-12-31",
        expected_source_docs=["halcyon-hb207-nda-summary.txt"],
        expected_entities=["sNDA-224567/S-003", "under review"],
    ),
    # ------------------------------------------------------------------
    # Provenance (2)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="pharma-q19",
        question=(
            "What is the citation basis for Form 483 observation 4 raised at "
            "the Meridian Biopharm Bedford site?"
        ),
        difficulty="provenance",
        hops=[
            Hop(
                subject_type="InspectionObservation",
                predicate="noted_at",
                object_type="ManufacturingSite",
            ),
        ],
        expected_source_docs=["meridian-biopharm-form483.txt"],
        expected_entities=["Observation 4", "21 CFR 211.100", "change-control"],
    ),
    GradedQuestion(
        id="pharma-q20",
        question="Which source document establishes that ORT-451 targets PD-L1?",
        difficulty="provenance",
        hops=[Hop(subject_type="Drug", predicate="targets", object_type="MoleculeTarget")],
        expected_source_docs=["ort451-nct9999001-record.txt"],
        expected_entities=["ORT-451", "PD-L1"],
    ),
]
