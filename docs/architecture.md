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

`ontos serve --snapshot <path>` boots the same pipeline over a frozen `NetworkxStore` pickle instead of a live store, with a `snapshot_info` tool reporting the file's ontology stamp. All existing read tools, `as_of`, ACL filtering, and the audit chain work unchanged.

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

## Caller authentication

Permission checks are only as good as the identity they are asked about. `ONTOS_AUTH_MODE` (`ontos/runtime/auth.py`, #69) decides where that identity comes from:

- **`none`** (default): `agent_identity` and `acting_on_behalf_of` are tool arguments, so they are *asserted*, not verified. The server refuses to bind a non-loopback address in this mode unless `ONTOS_ALLOW_UNAUTHENTICATED_BIND=1` is set. That opt-in is meant for the local docker-compose stack only.
- **`jwt`**: every HTTP request must carry a bearer JWT signed by the customer's IdP. The token is verified against `ONTOS_AUTH_ISSUER` and `ONTOS_AUTH_AUDIENCE`, using either a pinned `ONTOS_AUTH_PUBLIC_KEY_FILE` (no network access) or `ONTOS_AUTH_JWKS_URI` (https, the customer-configured IdP only). The **principal** is the token's `sub`. The **agent** is the RFC 8693 `act.sub` claim, falling back to the token's client id. A caller-supplied `acting_on_behalf_of` that disagrees with `sub` is rejected; to act for a user, an agent obtains a delegated token from the IdP (token exchange).

Every Article-12 record starts with an `authn` policy decision stating the method, and in `jwt` mode also the verified subject, agent and issuer. Rejections are audited at two layers:

- `AuthRejectionAuditMiddleware` records requests the transport turns away with 401/403. The token value is never recorded.
- Each tool records calls that reach it without a verified token, for example over an in-process transport.

The CLI is a trusted local operator: commands such as `ontos export --acting-on-behalf-of` still take an asserted identity.

Lookups by id follow the same permission rules as search (#78). `provenance` reads through `GraphStore.get_visible_fact`, which applies the caller's ACLs inside the store query. `audit_lookup` returns only the caller's own records unless the caller holds `audit_exporter` on `audit`. A forbidden record and a missing one produce the same response and the same audit outcome.

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
