# Security policy

Ontos targets regulated-industry deploys (financial services, health, pharma, legal, government, defense). Vulnerabilities in the audit chain, permission-aware traversal, or sovereignty guarantees are treated as **critical severity** regardless of exploitability, because customer procurement depends on those properties.

## Reporting a vulnerability

**Do NOT open a public GitHub issue.**

Email: **contact@getenclave.ai** with the subject line `[Ontos security]` so ops can route it correctly.

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce (a minimal proof-of-concept is ideal but not required).
- The Ontos version / commit SHA you tested against.
- Your name / handle for credit (optional; we're happy to keep you anonymous).

If you need encrypted communication, request our PGP key in your initial email and we will send it before you share details.

## What to expect

| Timeframe | What happens |
|---|---|
| **24 hours** | We acknowledge receipt. |
| **72 hours** | We confirm whether the report is in scope and give you an initial severity assessment. |
| **7 days** | We share the remediation plan and expected fix date. |
| **≤90 days** | We aim to ship a patched release; longer only for genuinely hard fixes, and we'll keep you updated. |
| **On release** | We credit you in the release notes and (with your permission) publish a security advisory via GitHub Security Advisories. |

## In-scope

- Anything in this repository (`ontos/`).
- Public documented deployment paths (Docker / Docker Compose / Helm).
- Interaction between Ontos and its documented dependencies (Neo4j, OpenFGA, SpiceDB, FastMCP) when the flaw is on the Ontos side of the interface.

## Out of scope

- Vulnerabilities in upstream dependencies with no Ontos-side aggravating factor — please report those upstream. If you find one that Ontos should mitigate defensively, we do want to hear about it.
- Findings against the dev-mode default audit signing key (`dev-only-signing-key-do-not-use`) — that string is intentionally a loud placeholder; production deploys must derive a real key from KMS or the customer's key vault. Reports against it will be closed as informational.
- Social-engineering attacks against the Enclave organization or its contributors.
- Denial-of-service via unbounded resource consumption when the operator has explicitly disabled the relevant limits.

## Especially interested in

We pay extra attention to reports that touch:

- **Audit-chain tampering** — any way to alter, drop, reorder, or forge an `AuditRecord` without the `verify_chain()` catching it.
- **Permission-aware traversal leaks** — any way an unauthorized subject can infer the existence, id, name, or shape of a forbidden node, or the count/structure of forbidden edges.
- **Bitemporal correctness violations** — any way a query with `as_of=T` returns a fact that was not valid at T.
- **Sovereignty violations** — any outbound network call from the runtime to a host the operator did not explicitly configure. Any telemetry, phone-home, or third-party callback in the request path.
- **Provenance forgery** — any way to produce a `Fact` whose provenance metadata bypasses the validation.

## Disclosure

We prefer coordinated disclosure. We will not take legal action against researchers who follow this policy in good faith, and we do not require an NDA before you report.
