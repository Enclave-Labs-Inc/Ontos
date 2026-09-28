# Fintech sample corpus — SYNTHETIC

These five documents are **hand-authored synthetic 10-K excerpts and
one 8-K**. All company names, CIK numbers, tickers, executives,
model names, and figures are fabricated to exercise the fintech
ontology (`../fintech.yaml`) — nothing in this directory should be
mistaken for a real SEC filing.

## Why synthetic

We chose synthetic over real SEC EDGAR filings for the first
corpus because:
- No copyright or licensing question (SEC filings are public
  domain, but well-known real issuers' names carry reputational
  ambiguity we'd rather avoid in a demo corpus).
- Precise coverage — we can guarantee the corpus exercises every
  load-bearing entity and multi-hop chain in the ontology.
- Small size (~15 KB total) keeps `pip install enclave-ontos`
  small; real EDGAR pulls come from a downloader added in a
  later PR.

## What's in each file

Every filing is written for fiscal year 2024 (calendar year 2024
except VDS which uses a Sept-30 fiscal year, and MBG's 8-K which
is dated 2025 Q1 to cross fiscal boundaries).

| File | Issuer | Ticker | CIK | What it exercises |
|---|---|---|---|---|
| `mbg-10k-fy2024.txt` | Meridian Bank Group | MBG | 0009900001 | Regulated bank — SR 11-7 model risk (Wholesale-Credit-PD-v3), Fed/OCC/FDIC oversight, board committees, credit + cyber risk |
| `nrtx-10k-fy2024.txt` | Nortex Semiconductor | NRTX | 0009900002 | Supply-chain risk (Taiwan foundry concentration), export controls, competitive landscape |
| `hih-10k-fy2024.txt` | Halcyon Insurance Holdings | HIH | 0009900003 | Insurer with SR 11-7 pricing models, catastrophe risk, cross-jurisdiction regulation. Shares auditor (Blackpine LLP) with MBG |
| `vds-10k-fy2024.txt` | Vanta Defense Systems | VDS | 0009900004 | Defense prime — ITAR/export controls, DoD-concentration risk, single-source supplier risk |
| `mbg-8k-2025q1.txt` | Meridian Bank Group | MBG | 0009900001 | 8-K trigger event (executive departure), cross-filing reference back to the 10-K |

## Cross-file entities (for entity-resolution testing)

Some entities appear in more than one filing to exercise ER:

- **Blackpine LLP** — auditor for both MBG and HIH
- **Cormorant Advisors LLP** — auditor for both NRTX and VDS
- **Federal Reserve** — regulator of MBG (directly) and HIH (via US bank subsidiaries)
- **Priya Krishnan** — MBG director in the 10-K; interim CFO in the 8-K
- **SR 11-7** — regulation referenced by both MBG and HIH model-control disclosures

## How the graded questions bind

The graded questions in `tests/verticals/test_fintech_graded.py`
(landing in M7.d) reference these files by name. If you rename a
file or remove one of the entities above, the graded tests will
fail loudly — that's the intent.
