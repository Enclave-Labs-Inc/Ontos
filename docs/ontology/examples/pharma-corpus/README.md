# Pharma sample corpus — SYNTHETIC

These five documents are **hand-authored synthetic** — one
ClinicalTrials.gov-style trial record, one FDA structured drug
label, one FDA Form 483 inspection observation report, one NDA
regulatory-submission summary, and one Phase 2 adverse-events
summary. All company names, NCT IDs, drug INNs, FDA establishment
IDs, submission numbers, and clinical figures are fabricated to
exercise the pharma ontology (`../pharma.yaml`) — nothing in this
directory should be mistaken for a real regulatory filing.

## Why synthetic

Same reasoning as the fintech corpus. ClinicalTrials.gov records
are public and FDA-483 observation excerpts are usually FOIA-released
in redacted form, but synthetic content gives us:
- guaranteed coverage of every load-bearing entity and multi-hop
  chain in the ontology;
- no ambiguity about identifying real inspection findings against
  real manufacturers;
- small footprint (~15 KB total).

## What's in each file

| File | What it is | What it exercises |
|---|---|---|
| `ort451-nct9999001-record.txt` | Phase 3 trial record for ORT-451 (INN meraltacept), NCT9999001 | Sponsor–CRO–PI–site chain, drug–target–indication triangle, protocol version tracking |
| `hb207-fda-label.txt` | FDA structured drug label for HB-207 (halomectinib, brand Xyleva) | Approved indication, adverse events on label, two manufacturing sites (in-house + contract) |
| `meridian-biopharm-form483.txt` | FDA Form 483 issued to Meridian Biopharm - Bedford MA | 4 inspection observations, GMP citations (21 CFR 211.192/211.113/211.100), classifications |
| `halcyon-hb207-nda-summary.txt` | NDA-224567 regulatory submission summary | Company → RegulatorySubmission → Regulator → Drug chain; approval decision + supplemental sNDAs |
| `ort451-phase2-sae-summary.txt` | Phase 2 SAE summary for ORT-451 (NCT9999000) | AdverseEvent → caused_by Drug; multi-site SAE reporting |

## Cross-file entities (for entity-resolution testing)

- **Orion Therapeutics Inc** — sponsor in `ort451-nct9999001-record.txt` and `ort451-phase2-sae-summary.txt`
- **ORT-451 (meraltacept)** — investigational drug in both trial documents
- **Meridian Biopharm - Bedford MA** — contract manufacturer for HB-207 (in the label AND named in the Form 483)
- **Halcyon Bio Corp** — marketing authorization holder for HB-207 (in the label AND the NDA summary)
- **FDA** — regulator in the label, Form 483, and NDA summary
- **21 CFR Part 210/211 (cGMP)** — regulation cited on both the label (site certification) and the Form 483 (citation basis)

## How the graded questions bind

Same as fintech — the graded questions in
`tests/verticals/test_pharma_graded.py` (landing in M7.d) reference
these files by name and expect specific multi-hop chains through
them.
