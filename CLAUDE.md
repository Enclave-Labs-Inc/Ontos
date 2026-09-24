# ontos — agent conventions

## What this repo is

**Ontos is a subset of Enclave (getenclave.ai), not an independent project.** It is the **knowledge-graph layer** of Enclave's sovereign AI company brain — the component that turns an enterprise's data into typed entities and typed, provenance-tagged relationships that LLM agents can query inside the customer's VPC.

Ontos sits alongside `enclave-runtime` (retrieval), `enclave-scribe` (extraction, in development), `enclave-ocr` (document/image OCR), and the product-surface repos under `/Users/alias1623/enclave/`.

Every fact stored has provenance. Every query emits an EU AI Act Article-12 audit record. Every traversal enforces per-edge permissions before the executor expands a successor node. Every deployment runs inside the customer's VPC — no outbound calls, no third-party dependencies in the data path.

## Non-negotiables

- **Provenance on every fact.** Never drop or synthesize provenance metadata. If you can't cite the source, don't return the fact.
- **Audit-first.** Every code path that touches a query MUST emit an audit event before returning. The audit emitter is not optional and not `try/except`ed away.
- **Permission-aware traversal.** Never post-filter for permissions when a pre-filter is possible. Never leak the *existence* of a forbidden node (no counts, no "hidden" markers).
- **Bitemporal.** Every write records `(t_valid, t_invalid, ingested_at, superseded_by)`. Never overwrite a fact in place — close its validity window and write a new one.
- **Sovereign.** No outbound network calls from the runtime to anything the customer didn't configure. No telemetry to Enclave-operated services from within the runtime.

## Stack conventions

- **Python 3.12+**, managed with **uv**.
- **FastMCP** for the MCP server surface — decorator API, streamable HTTP transport.
- **Pydantic v2** for all data models.
- **structlog** for logging (JSON structured, feeds audit sink).
- **pytest + pytest-asyncio** for tests.
- **ruff + mypy strict** for lint/type.

## Storage

- **M0:** in-memory NetworkX (dev only; safe local iteration; no persistence).
- **M1+:** Neo4j default; LadybugDB (Kuzu fork) optional embedded; Neptune for AWS-only shops.
- Storage is always accessed through `ontos.storage.base.GraphStore` — never call the driver directly from tools/executor.

## Extraction (temporary — will migrate to Enclave Scribe)

- **Today:** `ontos/extraction/` wraps LlamaIndex PropertyGraphIndex + LangChain LLMGraphTransformer. This is a bridge, not a long-term choice.
- **Migration target:** `enclave-scribe` (Enclave's sovereign in-VPC extraction model, sibling repo). Ontos swaps to Scribe once Scribe passes benchmarks against current frontier models on the extraction tasks Ontos uses (entity + relation extraction, provenance-confidence calibration, Text2Cypher accuracy).
- **Constraint on the extraction interface:** design `ontos.extraction` so a Scribe backend is a drop-in. Do NOT couple callers to LlamaIndex- or LangChain-specific types. The provenance wrapper (labels + confidence + extractor id/version) is the boundary — Scribe will populate the same shape.
- **Why this matters:** LlamaIndex + frontier-LLM API calls send extraction prompts to third-party clouds, violating Enclave's in-VPC promise. Scribe running inside the customer VPC closes the sovereignty loop.

## Testing

- Unit tests live next to modules under `tests/unit/`.
- Integration tests boot the FastMCP server and exercise it as a client.
- **`tests/compliance/`** contains authz-leak tests and Article-12 field-completeness tests. These are gate-blocking — a failure here MUST fail CI.
- **`tests/benchmarks/`** runs LOCOMO, HotpotQA, CypherBench-shaped tests. These do not gate CI but track regressions.

## Coding norms

- Public functions get docstrings; private ones only when non-obvious.
- No comments explaining *what* the code does — names should carry that. Comments only for *why* (invariants, subtle constraints, workarounds).
- Never catch broad exceptions in the request path without logging + audit emission.
- Never mutate a `Fact` in place. Facts are immutable; use `.superseded_by(new_fact)`.
