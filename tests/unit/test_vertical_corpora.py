"""M7.c — vertical sample corpora regression tests.

Locks the shape of the shipped fintech and pharma sample corpora
under `docs/ontology/examples/*-corpus/` so:

- the file names the graded questions in M7.d bind to don't drift,
- the cross-file entities the READMEs promise (Blackpine LLP as
  auditor for both MBG and HIH, Meridian Biopharm appearing in both
  the FDA label and the Form 483, etc.) actually exist in the text,
- every synthetic document keeps its "SYNTHETIC" safety marker so
  a well-meaning contributor can't quietly swap in a real filing.

These are string-level asserts on the corpus text, not extractor
tests — extraction quality lives in the benchmark harness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ontos.ingest.base import SourceDocument
from ontos.ingest.text import TextConnector

REPO_ROOT = Path(__file__).resolve().parents[2]
FINTECH_DIR = REPO_ROOT / "docs" / "ontology" / "examples" / "fintech-corpus"
PHARMA_DIR = REPO_ROOT / "docs" / "ontology" / "examples" / "pharma-corpus"

FINTECH_FILES = (
    "mbg-10k-fy2024.txt",
    "nrtx-10k-fy2024.txt",
    "hih-10k-fy2024.txt",
    "vds-10k-fy2024.txt",
    "mbg-8k-2025q1.txt",
)
PHARMA_FILES = (
    "ort451-nct9999001-record.txt",
    "hb207-fda-label.txt",
    "meridian-biopharm-form483.txt",
    "halcyon-hb207-nda-summary.txt",
    "ort451-phase2-sae-summary.txt",
)

SYNTHETIC_MARKER = "SYNTHETIC DEMONSTRATION DOCUMENT"


def _load_corpus_texts(directory: Path, filenames: tuple[str, ...]) -> dict[str, str]:
    return {name: (directory / name).read_text() for name in filenames}


async def _collect(connector: TextConnector) -> list[SourceDocument]:
    docs: list[SourceDocument] = []
    stream: AsyncIterator[SourceDocument] = connector.iter_documents()
    async for doc in stream:
        docs.append(doc)
    return docs


# --------------------------------------------------------------------
# File presence + integrity
# --------------------------------------------------------------------


def test_fintech_corpus_files_all_present() -> None:
    for name in FINTECH_FILES:
        p = FINTECH_DIR / name
        assert p.is_file(), f"missing fintech corpus file: {name}"
        assert p.stat().st_size > 500, f"suspiciously small: {name}"


def test_pharma_corpus_files_all_present() -> None:
    for name in PHARMA_FILES:
        p = PHARMA_DIR / name
        assert p.is_file(), f"missing pharma corpus file: {name}"
        assert p.stat().st_size > 500, f"suspiciously small: {name}"


def test_fintech_corpus_has_readme() -> None:
    assert (FINTECH_DIR / "README.md").is_file()


def test_pharma_corpus_has_readme() -> None:
    assert (PHARMA_DIR / "README.md").is_file()


# --------------------------------------------------------------------
# Synthetic-marker safety net — nobody swaps in a real filing
# --------------------------------------------------------------------


@pytest.mark.parametrize("filename", FINTECH_FILES)
def test_every_fintech_doc_declares_synthetic(filename: str) -> None:
    text = (FINTECH_DIR / filename).read_text()
    assert SYNTHETIC_MARKER in text, (
        f"{filename} lost its SYNTHETIC marker — refuse to ship a "
        "corpus that might be mistaken for a real SEC filing"
    )


@pytest.mark.parametrize("filename", PHARMA_FILES)
def test_every_pharma_doc_declares_synthetic(filename: str) -> None:
    text = (PHARMA_DIR / filename).read_text()
    assert SYNTHETIC_MARKER in text, (
        f"{filename} lost its SYNTHETIC marker — refuse to ship a "
        "corpus that might be mistaken for a real regulatory filing"
    )


# --------------------------------------------------------------------
# Loadable via TextConnector — proves the ingest pipeline sees them
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fintech_corpus_loads_via_text_connector() -> None:
    texts = _load_corpus_texts(FINTECH_DIR, FINTECH_FILES)
    connector = TextConnector(texts)
    docs = await _collect(connector)
    assert len(docs) == len(FINTECH_FILES)
    assert {d.source_id for d in docs} == set(FINTECH_FILES)
    for d in docs:
        assert d.text.strip()


@pytest.mark.asyncio
async def test_pharma_corpus_loads_via_text_connector() -> None:
    texts = _load_corpus_texts(PHARMA_DIR, PHARMA_FILES)
    connector = TextConnector(texts)
    docs = await _collect(connector)
    assert len(docs) == len(PHARMA_FILES)
    assert {d.source_id for d in docs} == set(PHARMA_FILES)
    for d in docs:
        assert d.text.strip()


# --------------------------------------------------------------------
# Cross-file entity anchors — pin the ER-worthy repeats the READMEs promise
# --------------------------------------------------------------------


def test_fintech_shared_auditor_blackpine_in_mbg_and_hih() -> None:
    mbg = (FINTECH_DIR / "mbg-10k-fy2024.txt").read_text()
    hih = (FINTECH_DIR / "hih-10k-fy2024.txt").read_text()
    assert "Blackpine LLP" in mbg
    assert "Blackpine LLP" in hih


def test_fintech_shared_auditor_cormorant_in_nrtx_and_vds() -> None:
    nrtx = (FINTECH_DIR / "nrtx-10k-fy2024.txt").read_text()
    vds = (FINTECH_DIR / "vds-10k-fy2024.txt").read_text()
    assert "Cormorant Advisors LLP" in nrtx
    assert "Cormorant Advisors LLP" in vds


def test_fintech_shared_regulation_sr_11_7_in_mbg_and_hih() -> None:
    mbg = (FINTECH_DIR / "mbg-10k-fy2024.txt").read_text()
    hih = (FINTECH_DIR / "hih-10k-fy2024.txt").read_text()
    assert "SR 11-7" in mbg
    assert "SR 11-7" in hih


def test_fintech_priya_krishnan_in_both_mbg_filings() -> None:
    ten_k = (FINTECH_DIR / "mbg-10k-fy2024.txt").read_text()
    eight_k = (FINTECH_DIR / "mbg-8k-2025q1.txt").read_text()
    assert "Priya Krishnan" in ten_k
    assert "Priya Krishnan" in eight_k


def test_fintech_taiwan_supply_chain_risk_in_nrtx() -> None:
    # The single most important risk disclosure in the fintech corpus —
    # will be the anchor of a canonical graded question in M7.d.
    nrtx = (FINTECH_DIR / "nrtx-10k-fy2024.txt").read_text()
    assert "Taiwan" in nrtx
    assert "supply" in nrtx.lower() and "chain" in nrtx.lower()


def test_pharma_meridian_biopharm_in_label_and_form483() -> None:
    label = (PHARMA_DIR / "hb207-fda-label.txt").read_text()
    form483 = (PHARMA_DIR / "meridian-biopharm-form483.txt").read_text()
    assert "Meridian Biopharm" in label
    assert "Meridian Biopharm" in form483


def test_pharma_ort451_in_trial_and_sae_summary() -> None:
    trial = (PHARMA_DIR / "ort451-nct9999001-record.txt").read_text()
    sae = (PHARMA_DIR / "ort451-phase2-sae-summary.txt").read_text()
    assert "ORT-451" in trial
    assert "ORT-451" in sae
    # Both should also reference the sponsor.
    assert "Orion Therapeutics" in trial
    assert "Orion Therapeutics" in sae


def test_pharma_hb207_in_label_and_nda_summary() -> None:
    label = (PHARMA_DIR / "hb207-fda-label.txt").read_text()
    nda = (PHARMA_DIR / "halcyon-hb207-nda-summary.txt").read_text()
    assert "halomectinib" in label
    assert "halomectinib" in nda
    assert "XYLEVA" in label
    assert "XYLEVA" in nda
    assert "Halcyon Bio" in label
    assert "Halcyon Bio" in nda


def test_pharma_cgmp_regulation_in_label_and_form483() -> None:
    label = (PHARMA_DIR / "hb207-fda-label.txt").read_text()
    form483 = (PHARMA_DIR / "meridian-biopharm-form483.txt").read_text()
    assert "21 CFR" in label
    assert "21 CFR" in form483


def test_pharma_nda_references_shared_manufacturing_site() -> None:
    # The NDA summary should mention the same FEI (3009654321) as the Form 483 —
    # this is the load-bearing GxP → NDA cross-source join the graded questions rely on.
    nda = (PHARMA_DIR / "halcyon-hb207-nda-summary.txt").read_text()
    form483 = (PHARMA_DIR / "meridian-biopharm-form483.txt").read_text()
    assert "3009654321" in nda
    assert "3009654321" in form483
