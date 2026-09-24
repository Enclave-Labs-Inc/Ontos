# EU AI Act Article 12 — field mapping

Article 12 of the EU AI Act (Regulation (EU) 2024/1689) requires providers of high-risk AI systems to maintain automatic recording of events over the system's lifetime. Enforcement for high-risk systems begins **August 2026**.

## What Article 12 requires

- **≥6 months minimum retention** of audit logs.
- **≥12 distinct fields** per AI-influenced decision.
- **Per-user attribution** — the named unsolved problem in the compliance literature is that AI systems often access regulated data under a service account, with no log recording which individual directed the access.
- **Tamper-evident** — regulators must be able to verify the log has not been altered.

## How `ontos` maps

Every tool call in `runtime/server.py` emits exactly one `AuditRecord`. Records are hash-chained (`prev_hash` → `hash`) and HMAC-signed with a per-deploy key. The 12 fields captured (`ArticleTwelveField` enum in `runtime/models.py`) are:

| Field | Source in the runtime |
|---|---|
| `query_id` | Generated per tool call |
| `agent_identity` | Passed in from the MCP client (e.g. `claude-code:session-abc`) |
| `acting_on_behalf_of` | The human identity the agent is acting for (e.g. `user:alice`) |
| `query_text` | The natural-language query or entity id the tool received |
| `tool_invoked` | `search` / `traverse` / `explain` / `provenance` / `audit` / `as_of` |
| `tool_arguments` | JSON dump of all arguments (excluding secrets) |
| `result_fact_ids` | The exact fact ids returned to the agent |
| `result_hash` | SHA-256 of the sorted result-fact-ids list |
| `timestamp` | UTC ISO 8601 |
| `latency_ms` | Wall-clock time to serve the request |
| `model_versions` | Runtime + backend + (later) planner/executor model versions |
| `policy_decisions` | Every authz check made during the request (allow/deny + rule id) |

## What's not solved yet (roadmap)

- **M0:** in-memory chain (validates the shape).
- **M2:** persistent hash-chained log in Postgres; 6-month retention enforcement; export to CloudTrail / S3.
- **M3+:** external verifier CLI (`ontos audit verify`) that walks the chain and reports tampering; per-query "prove the answer respected these ACLs at that timestamp" attestation.
