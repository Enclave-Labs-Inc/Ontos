"""20 graded questions for the fintech vertical.

Each question binds to the fintech ontology + corpus and covers one
of five difficulty tiers: single-hop (5), multi-hop (7), cross-source
(4), temporal (2), provenance (2). Distribution is intentional — the
static test suite pins these tier counts so a well-meaning edit that
kills a tier fails loud.
"""

from __future__ import annotations

from tests.verticals._schema import GradedQuestion, Hop

FINTECH_QUESTIONS: list[GradedQuestion] = [
    # ------------------------------------------------------------------
    # Single-hop (5)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="fintech-q01",
        question="Who is the independent auditor of Meridian Bank Group for fiscal year 2024?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Company", predicate="audited_by", object_type="Auditor")],
        expected_source_docs=["mbg-10k-fy2024.txt"],
        expected_entities=["Meridian Bank Group", "Blackpine LLP"],
    ),
    GradedQuestion(
        id="fintech-q02",
        question="Which regulators oversee Halcyon Insurance Holdings?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Company", predicate="regulated_by", object_type="Regulator")],
        expected_source_docs=["hih-10k-fy2024.txt"],
        expected_entities=[
            "Halcyon Insurance Holdings",
            "Bermuda Monetary Authority",
            "Federal Reserve",
        ],
    ),
    GradedQuestion(
        id="fintech-q03",
        question="What business segments does Vanta Defense Systems report?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Company", predicate="has_segment", object_type="BusinessSegment")],
        expected_source_docs=["vds-10k-fy2024.txt"],
        expected_entities=["Aerospace", "Ground Systems", "Cyber Solutions"],
    ),
    GradedQuestion(
        id="fintech-q04",
        question="Which board committees does Meridian Bank Group operate?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Company", predicate="has_committee", object_type="BoardCommittee")],
        expected_source_docs=["mbg-10k-fy2024.txt"],
        expected_entities=[
            "Audit Committee",
            "Risk Committee",
            "Compensation Committee",
            "Nominating & Governance Committee",
        ],
    ),
    GradedQuestion(
        id="fintech-q05",
        question="In which jurisdictions does Nortex Semiconductor operate design centers?",
        difficulty="single_hop",
        hops=[Hop(subject_type="Company", predicate="operates_in", object_type="Jurisdiction")],
        expected_source_docs=["nrtx-10k-fy2024.txt"],
        expected_entities=["Taiwan", "Japan", "Germany"],
    ),
    # ------------------------------------------------------------------
    # Multi-hop (7)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="fintech-q06",
        question=(
            "Which 10-K filings in the corpus disclose a supply-chain risk "
            "originating in Taiwan?"
        ),
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="filed", object_type="Filing"),
            Hop(subject_type="Filing", predicate="contains_section", object_type="FilingSection"),
            Hop(subject_type="FilingSection", predicate="discloses_risk", object_type="Risk"),
        ],
        expected_source_docs=["nrtx-10k-fy2024.txt"],
        expected_entities=["Nortex Semiconductor", "Taiwan", "supply chain"],
    ),
    GradedQuestion(
        id="fintech-q07",
        question=(
            "Which tier-1 model does Meridian Bank Group operate under SR 11-7, "
            "and what control class governs it?"
        ),
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="operates_model", object_type="Model"),
            Hop(subject_type="Model", predicate="governed_by", object_type="ModelControl"),
            Hop(
                subject_type="ModelControl",
                predicate="references_regulation",
                object_type="Regulation",
            ),
        ],
        expected_source_docs=["mbg-10k-fy2024.txt"],
        expected_entities=["Wholesale-Credit-PD-v3", "SR 11-7", "independent"],
    ),
    GradedQuestion(
        id="fintech-q08",
        question="Which director of Meridian Bank Group chairs the Risk Committee?",
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="has_committee", object_type="BoardCommittee"),
            Hop(subject_type="Person", predicate="sits_on_committee", object_type="BoardCommittee"),
        ],
        expected_source_docs=["mbg-10k-fy2024.txt"],
        expected_entities=["Michael O. Okafor", "Risk Committee"],
    ),
    GradedQuestion(
        id="fintech-q09",
        question=(
            "Which regulations enforced by the Federal Reserve does "
            "Meridian Bank Group subject_to?"
        ),
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="subject_to", object_type="Regulation"),
            Hop(subject_type="Regulation", predicate="enforced_by", object_type="Regulator"),
        ],
        expected_source_docs=["mbg-10k-fy2024.txt"],
        expected_entities=["SR 11-7", "Dodd-Frank", "Federal Reserve"],
    ),
    GradedQuestion(
        id="fintech-q10",
        question="What SR 11-7 tier-1 pricing model does Halcyon Insurance disclose?",
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="operates_model", object_type="Model"),
            Hop(subject_type="Model", predicate="governed_by", object_type="ModelControl"),
        ],
        expected_source_docs=["hih-10k-fy2024.txt"],
        expected_entities=["Property-Catastrophe-Pricer-v2", "SR 11-7"],
    ),
    GradedQuestion(
        id="fintech-q11",
        question="Which principal competitors of Nortex Semiconductor are named in its 10-K?",
        difficulty="multi_hop",
        hops=[Hop(subject_type="Company", predicate="competes_with", object_type="Company")],
        expected_source_docs=["nrtx-10k-fy2024.txt"],
        expected_entities=["Cerulean Compute", "Halcyon Silicon"],
    ),
    GradedQuestion(
        id="fintech-q12",
        question="What material risk factors does Halcyon Insurance disclose in its FY2024 10-K?",
        difficulty="multi_hop",
        hops=[
            Hop(subject_type="Company", predicate="filed", object_type="Filing"),
            Hop(subject_type="Filing", predicate="contains_section", object_type="FilingSection"),
            Hop(subject_type="FilingSection", predicate="discloses_risk", object_type="Risk"),
        ],
        expected_source_docs=["hih-10k-fy2024.txt"],
        expected_entities=["Catastrophe risk", "climate", "Model risk"],
    ),
    # ------------------------------------------------------------------
    # Cross-source (4)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="fintech-q13",
        question="Which companies in the corpus are audited by Blackpine LLP?",
        difficulty="cross_source",
        hops=[Hop(subject_type="Company", predicate="audited_by", object_type="Auditor")],
        expected_source_docs=["mbg-10k-fy2024.txt", "hih-10k-fy2024.txt"],
        expected_entities=[
            "Meridian Bank Group",
            "Halcyon Insurance Holdings",
            "Blackpine LLP",
        ],
    ),
    GradedQuestion(
        id="fintech-q14",
        question="Which companies in the corpus are audited by Cormorant Advisors LLP?",
        difficulty="cross_source",
        hops=[Hop(subject_type="Company", predicate="audited_by", object_type="Auditor")],
        expected_source_docs=["nrtx-10k-fy2024.txt", "vds-10k-fy2024.txt"],
        expected_entities=[
            "Nortex Semiconductor",
            "Vanta Defense Systems",
            "Cormorant Advisors LLP",
        ],
    ),
    GradedQuestion(
        id="fintech-q15",
        question="Which company in the corpus filed both a 10-K and an 8-K?",
        difficulty="cross_source",
        hops=[Hop(subject_type="Company", predicate="filed", object_type="Filing")],
        expected_source_docs=["mbg-10k-fy2024.txt", "mbg-8k-2025q1.txt"],
        expected_entities=["Meridian Bank Group", "10-K", "8-K"],
    ),
    GradedQuestion(
        id="fintech-q16",
        question=(
            "Which SR 11-7 regulation is invoked by both a bank and an insurer "
            "in the corpus?"
        ),
        difficulty="cross_source",
        hops=[Hop(subject_type="Company", predicate="subject_to", object_type="Regulation")],
        expected_source_docs=["mbg-10k-fy2024.txt", "hih-10k-fy2024.txt"],
        expected_entities=["SR 11-7", "Meridian Bank Group", "Halcyon Insurance"],
    ),
    # ------------------------------------------------------------------
    # Temporal (2)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="fintech-q17",
        question="Who was Chief Financial Officer of Meridian Bank Group as of February 1, 2025?",
        difficulty="temporal",
        hops=[
            Hop(subject_type="Person", predicate="serves_as_officer_of", object_type="Company"),
        ],
        as_of="2025-02-01",
        expected_source_docs=["mbg-10k-fy2024.txt", "mbg-8k-2025q1.txt"],
        expected_entities=["Diane Xu"],
        notes=(
            "Before the interim CFO effective date of Feb 16, 2025; "
            "should still return Diane Xu, not Priya Krishnan."
        ),
    ),
    GradedQuestion(
        id="fintech-q18",
        question=(
            "Who was Interim Chief Financial Officer of Meridian Bank Group "
            "as of February 20, 2025?"
        ),
        difficulty="temporal",
        hops=[
            Hop(subject_type="Person", predicate="serves_as_officer_of", object_type="Company"),
        ],
        as_of="2025-02-20",
        expected_source_docs=["mbg-8k-2025q1.txt"],
        expected_entities=["Priya Krishnan"],
        notes="After Diane Xu's departure (Feb 15) and the interim appointment (Feb 16).",
    ),
    # ------------------------------------------------------------------
    # Provenance (2)
    # ------------------------------------------------------------------
    GradedQuestion(
        id="fintech-q19",
        question=(
            "In which document and section is Nortex Semiconductor's Taiwan "
            "supply-chain risk disclosed?"
        ),
        difficulty="provenance",
        hops=[
            Hop(subject_type="Company", predicate="filed", object_type="Filing"),
            Hop(subject_type="Filing", predicate="contains_section", object_type="FilingSection"),
            Hop(subject_type="FilingSection", predicate="discloses_risk", object_type="Risk"),
        ],
        expected_source_docs=["nrtx-10k-fy2024.txt"],
        expected_entities=["ITEM 1A", "RISK FACTORS", "Supply chain", "Taiwan"],
    ),
    GradedQuestion(
        id="fintech-q20",
        question="Which filing announced Diane Xu's departure as CFO of Meridian Bank Group?",
        difficulty="provenance",
        hops=[Hop(subject_type="Company", predicate="filed", object_type="Filing")],
        expected_source_docs=["mbg-8k-2025q1.txt"],
        expected_entities=["FORM 8-K", "Diane Xu", "February 1, 2025"],
    ),
]
