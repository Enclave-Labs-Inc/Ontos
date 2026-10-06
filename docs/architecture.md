# Architecture

## Where Ontos fits in Enclave

Ontos is one component of Enclave (getenclave.ai). It provides the **knowledge-graph layer** that the sovereign AI company brain reasons over. Sibling components:

- `enclave-runtime` — retrieval substrate (Rust shard workers, HNSW-over-S3, layer-0 cache).
- `enclave-scribe` — sovereign extraction model, in development. Once it beats current frontier models on Ontos's extraction workload, it replaces the LlamaIndex/LangChain wrappers in `ontos/extraction/`. Until then, `ontos/extraction/` is a bridge; the extractor interface is designed as a drop-in for Scribe.
- `enclave-ocr` — document + image OCR.
- Product-surface repos (`enclave-gtm`, `enclave-home`, `enclave-business`, etc.) consume Ontos via MCP.

## Pipeline

```
Sources → Extractors → Provenance-tagged Facts → GraphStore → Executor → MCP Tools → Agent
                                                              ↓
                                                          Authz layer
                                                              ↓
                                                          Audit sink
```

Every request path passes through the audit sink before returning. No exceptions.

## Module contracts

| Module | Public entry | Input → Output |
|---|---|---|
| `runtime/server.py` | `build_server(config, store, audit) -> FastMCP` | wires storage + audit into MCP tools |
| `runtime/models.py` | `Fact`, `Provenance`, `AuditRecord`, `ArticleTwelveField` | immutable data model; frozen Pydantic |
| `runtime/config.py` | `RuntimeConfig.from_env()` | env → typed config |
| `storage/base.py` | `GraphStore` Protocol | backend-agnostic contract |
| `storage/networkx_store.py` | `NetworkxStore` | M0 in-memory implementation |
| `audit/emitter.py` | `AuditEmitter` | hash-chained Article-12 log |

## Storage tiers

- **M0 (current):** `NetworkxStore` — in-memory MultiDiGraph. Dev-only.
- **M1:** Neo4j via the official `neo4j` Python driver. Default for regulated deploy.
- **M2 optional:** LadybugDB (community Kuzu fork) for embedded / zero-infra local runs.
- **M2 optional:** Amazon Neptune for AWS-only shops.

## Data model — Fact is immutable

Correcting a fact does *not* mutate the original. The correction is a new `Fact` and the old one is closed via `close_fact(fact_id, t_invalid, superseded_by=new_fact.id)`. This preserves the full history for bitemporal `as_of` queries and for the audit trail.

## Permission-aware traversal

`_acl_allows` is called *during* traversal in `NetworkxStore.traverse` — before any successor node is expanded. A forbidden edge is skipped and its target is never reached, so an unauthorized subject cannot infer the existence of a forbidden node from the response (no counts, no hidden markers).

M2 replaces the inline check with a call into OpenFGA (`Check(user, relation, object)`).

## Ontology evolution and lazy migration

Every `Provenance` carries `ontology_id` and `ontology_version`, stamped by `LlmExtractor` from the active `Ontology` at ingest. Readers plug an `ontos.migration.MigrationRegistry` into the executor to translate historical facts forward when the schema evolves (predicate rename, deprecation, cardinality tighten). The seam mirrors `SupersessionPolicy`: a Protocol-shaped `OntologyMigration` with `id`, `from_version`, `to_version`, and an async per-fact `migrate_fact` method.

The registry chains migrations across version hops via BFS; multiple migrations on the same edge apply in registration order (one version bump typically bundles rename + deprecate + tighten). Translation is **read-side only** — the stored fact's `predicate` and `provenance.ontology_version` never change, preserving the bitemporal immutability invariant. Migration events land in `ExecutionResult.warnings` so Article-12 audit tools can reconstruct which facts were translated or dropped and by which migration.

Strict mode (`MigrationRegistry(strict=True)`) refuses to show facts the current ontology can't vouch for; the default is pass-through-with-warning so pre-#34 (unstamped) facts stay visible during ingest-side rollout.

## Audit contract

Every tool in `runtime/server.py`:

1. Calls the store.
2. Computes latency.
3. Emits an `AuditRecord` via `AuditEmitter.emit(...)`.
4. Returns a `ToolResponse` wrapping the payload with `query_id` and `audit_hash`.

The audit emission happens **before** the return statement, in the same coroutine. There is no queue that could drop records, and no `try/except` that could swallow them.
