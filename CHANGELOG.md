# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every release ships:

- a matching git tag `vX.Y.Z`,
- a PyPI wheel + sdist at `enclave-ontos==X.Y.Z` (imported as `import ontos`; the PyPI dist name is `enclave-ontos` because the `ontos` name was taken on PyPI),
- a GHCR container image at `ghcr.io/enclave-labs-inc/ontos:X.Y.Z`,
- a Helm chart `appVersion` bumped to match under `deploy/helm/ontos/Chart.yaml`.

See [RELEASING.md](RELEASING.md) for how a release is cut.

## [Unreleased]

## [0.7.0] - 2026-10-29 - MCP authentication, rejected-candidate retention and ontology suggestions

MCP callers can now be authenticated with JWTs from the customer's own
IdP, and identity comes from the verified token instead of tool
arguments. Relationships the ontology rejects can be retained (opt-in)
in a separate candidate store, inspected, and turned into deterministic
`add_pattern` suggestions, with audited CLIs gated on
`ontology_reviewer` plus source-level VIEW. Entities now resolve by
canonical name and alias. Several security fixes land alongside:
permission checks on `provenance` / `audit_lookup`, keyed audit
references for candidates, a hardened export push guard, and an entity
name leak in Neo4j search. **Read the upgrade notes: this release has
breaking changes.**

### Upgrade notes
- **Breaking: an unauthenticated server no longer binds beyond
  loopback.** With `ONTOS_AUTH_MODE=none` (the default), `ontos serve`
  and `python -m ontos.runtime.server` refuse a non-loopback
  `ONTOS_LISTEN_HOST` such as `0.0.0.0`. To fix:
  - configure `ONTOS_AUTH_MODE=jwt`, or
  - set `ONTOS_ALLOW_UNAUTHENTICATED_BIND=1` (dev only).
  
  The bundled `docker-compose.yml` sets the opt-in. The Docker image does
  not, so a bare `docker run` needs one of the two.
- **Helm:** the chart now defaults to `auth.mode: jwt` and fails to
  render until `auth.issuer`, `auth.audience` and one key source
  (`auth.jwksUri` or `auth.publicKeySecretRef.name`) are set. To keep an
  unauthenticated deployment, set `auth.mode: none` and
  `auth.allowUnauthenticatedBind: true`.
- Every MCP Article-12 record now starts with an `authn` entry in
  `policy_decisions`. Consumers that read `policy_decisions[0]` as the
  authz decision should match on `policy` instead.
- **Custom `GraphStore` implementations must add `get_visible_fact(...)`**,
  the ACL-filtered fact lookup used by read surfaces. `get_fact` remains
  the unfiltered write-path lookup. Both bundled stores implement it.
- `provenance` and `audit_lookup` now enforce read permissions (#78).
  Callers relying on reading facts outside their ACLs, or other
  principals' audit records, now get `null`. Grant `audit_exporter` on
  `audit` (OpenFGA) to readers of other principals' records.
- **Custom `GraphStore` implementations must add
  `search_entities(query, *, as_of, k, acl_subject, allowed_acls)`** (#82).
  Both bundled stores implement it.

### Fixed — release packaging (Tier 0)
- **The container image now runs the Helm chart's defaults.** It installs
  the `neo4j` and `authz` (OpenFGA) extras from `uv.lock` in a two-stage
  build. Before, a default chart install (`ONTOS_STORAGE_BACKEND=neo4j`)
  failed at startup with `ModuleNotFoundError: No module named 'neo4j'`.
- **Neo4j schema setup now runs on the runtime path.** `ontos serve`, via
  the MCP server lifespan, and the `ingest`, `query`, `export`,
  `ontology candidates` and `ontology suggestions` commands call
  `Neo4jStore.initialize()`. Production databases previously ran without
  indexes, without the rejected-candidate uniqueness constraints, and
  without the candidate migration.
- **CI builds and smoke-tests the image on every change**: backend imports,
  non-root uid, and refusal of an unauthenticated public bind. The release
  workflow also checks that the Helm chart's `version`, `appVersion` and
  image tag match the release version.

### Fixed — entity lookup by name (#82)
- **`search` matches canonical names and aliases.** It now also matches the
  canonical names and aliases of a fact's endpoint entities, not only ids and
  the predicate. "Who works at Atlas Robotics?" found nothing when the
  company's id was `"2"`.
- **Both backends use the same matching rule.** Neo4j matched names but not
  ids; it now matches ids, the predicate, and names and aliases.
- **Security: Neo4j search leaked restricted entity names.** It matched the
  canonical name of an endpoint entity regardless of that entity's ACL, so a
  caller could probe the name of a restricted entity through a public fact.
  Names and aliases now match only for entities the caller may see.
- **New `GraphStore.search_entities` resolves entities by id, name or alias.**
  It matches only entities the caller can see that have at least one visible
  fact alive at `as_of`. Raw nodes carry no ACL of their own, so this keeps
  entities known only through forbidden facts hidden.
- **The `SeedByEntity` fallback resolves names through `search_entities`.** A
  popular entity can no longer crowd a namesake out of the 50-fact search
  window, which used to silently resolve to the wrong entity. The fallback
  still refuses to guess when several entities match.
- **The planner prompt no longer says stored ids are "typically" names.**

### Added — deterministic ontology suggestions (#66 M2a, #84)
- `ontos ontology suggestions list --ontology <yaml>` proposes `add_pattern`
  changes when a rejected candidate's relation and entity types already exist
  in the ontology but the directed pattern is missing.
- Suggestions are computed on demand from the caller's VIEW-filtered
  candidates and are never persisted or written to the trusted graph.
- Each suggestion comes from a single ACL; there is no cross-ACL aggregation.
- Support counts unique `(source, subject, object)` evidence. Supporting
  candidates are referenced only by keyed HMAC references.
- Candidates stamped with another ontology version are excluded.
- Access requires `ontology_reviewer` plus source VIEW, as for candidate
  inspection, and permissions are re-checked before generation. Every request,
  denial and failure is audited.
- CLI only, in a trusted local operator context. No model calls in M2a.

### Fixed — M2a follow-ups (#85)
- **Revocation is a denial.** Revoking the reviewer role or VIEW during a
  suggestions request is audited as `deny` and reported as "denied" (it was
  `authorization_error` / "failed").
- **Secondary audit failures are logged.** When the audit sink fails on the
  failure path too, the request still fails with its original error, and an
  `ontology-suggestions-failure-audit-failed` event records both error types.
- **`OntologySuggestion` validates kind against fields.** `add_pattern`
  requires a pattern; the M2b kinds are rejected until they are specified.

### Added — rejected-candidate retention and inspection (#66 M1, #68)
- **`ontos ingest --retain-rejected-candidates`** (off by default) keeps
  relationships that fail ontology validation as typed `RejectedCandidate`
  records. Each holds the raw triple, the rejection reason, partial
  provenance and the source document's ACL.
- **Candidates live in a separate `CandidateStore`**: NetworkX V3 pickles
  (V1/V2 still load) and Neo4j `:RejectedCandidate` nodes. They are never
  part of search, traversal, scans, exports, Bloom or snapshots.
- **`ontos ontology candidates list`** inspects them in a trusted local
  operator context. It requires the `ontology_reviewer` relation plus VIEW
  on each candidate's source ACL, and audits every attempt. There is no MCP
  tool for candidates yet.
- **Rejection warnings in ingest reports are sanitized**, so they no longer
  carry raw ids or predicates.

### Fixed — security
- **Candidate audit references are keyed (#72, #77).** Inspection logged
  unkeyed SHA-256 candidate ids and cursor hashes, which an audit reader
  could test guesses against. It now logs an HMAC under a subkey of the
  audit signing key (`AuditSink.derive_key`). The audit field
  `after_cursor_sha256` is renamed to `after_cursor_ref`.
- **The export push guard can no longer be bypassed (#74, #76).**
  `ontos export neo4j --push` could reach the operational database
  through a loopback or DNS alias of its host, a differently cased
  database name, or a NetworkX-sourced export. Hosts are now compared by
  every resolved identity, names case-insensitively, and the guard applies
  whenever an operational Neo4j is configured.

### Fixed — rejected-candidate retention follow-ups (#73, part 1)
- **Entity ids that change between runs no longer duplicate candidates.**
  Same-source entity reconciliation now remaps rejected candidates' raw ids,
  as it already did for entities and facts.
- **Candidates carry `ingested_at`**, set the first time they are retained
  and kept on later re-ingests. It is not part of candidate identity.
- **The pipeline stamps `provenance.source_id` from the document**, as it
  already did for `acl_ref`, so an extractor can't attribute candidates to
  another source.
- **`IngestReport.candidates_retained` counts only newly stored candidates.**
  `CandidateStore.add_candidates` now returns that count (an upgrade note for
  custom stores).
- **Candidate `source_id` must be non-empty.** An empty one couldn't
  round-trip through the pagination cursor.
- **NetworkX candidate storage is indexed.** Adding 10k candidates in
  batches of 20 dropped from about 15 s to 0.2 s, and paging all of them
  from about 12 s to 0.01 s. Storage-boundary validation now actually
  re-validates.
- **Neo4j gets a `(source_key, logical_id)` index for inspection paging.**
- **Both backends reject `limit=True`.**

### Fixed — permission checks on record lookups (#78)
- **`provenance(fact_id)`** returned any fact by id, whatever the caller's
  ACLs. It now resolves `allowed_acls` like `search` and filters in the
  store (`GraphStore.get_visible_fact`). It gained an optional
  `acting_on_behalf_of` argument for `ONTOS_AUTH_MODE=none`.
- **`audit_lookup(query_id)`** returned any principal's Article-12 record.
  It now returns only the caller's own records, or any record for holders
  of `audit_exporter` on `audit`.
- In both tools, a forbidden record gets the same `null` response and the
  same audit outcome as a nonexistent one. The caller can read its own
  audit records back, so a distinct outcome would reveal that the record
  exists.

### Fixed — candidate inspection CLI and audit (#73, part 2)
- **Setup failures in `ontos ontology candidates list` are now audited.**
  An unusable authz backend or candidate store writes an Article-12
  `authorization_error` / `storage_error` record and exits 1 with a short
  message, no traceback. If the audit store itself can't initialise, the
  command fails closed before reading anything.
- **Only real request errors (bad limit, bad cursor) report "Invalid
  candidate inspection request" (exit 2).** Storage and audit failures
  report "Candidate inspection failed" (exit 1), matching the audit trail.
- **`--limit` is range-checked by the audited inspection service**, so an
  out-of-range value leaves an `invalid_request` record, as the library
  already did.
- **A failing audit-sink close no longer turns a completed, audited listing
  into exit 1**, and can't mask an earlier error.
- **`serve --snapshot` reports the pickle's real format** (for example
  `ontos-nx-store-v3`) and warns that V3 dev stores can contain retained
  candidates.
- **The candidate compliance tests now assert the decision, subject and
  agent of each record**, not just that the fields are present.

### Added — MCP caller authentication (#69)
- `ONTOS_AUTH_MODE=jwt` verifies a bearer JWT on every MCP request
  against a customer-configured issuer and audience. The key source is
  either a pinned public key (`ONTOS_AUTH_PUBLIC_KEY_FILE`, no network
  access) or the IdP's JWKS over https (`ONTOS_AUTH_JWKS_URI`).
  Optional `ONTOS_AUTH_REQUIRED_SCOPES` and `ONTOS_AUTH_ALGORITHM`.
- The authorization principal is the token's `sub`; the agent is the
  RFC 8693 `act.sub` claim, or the client id. A caller can no longer
  read as another user by passing a different `acting_on_behalf_of`:
  a value that disagrees with the token is rejected.
- Rejections are audited, both at the transport (401/403, token never
  recorded) and per tool for calls that arrive without a verified token.
  Clients get a generic `authentication failed`.
- Gate-blocking compliance tests run a real HTTP server with JWT auth.
  They read the tool list from the server, so every newly registered
  tool is covered automatically.

## [0.6.0] - 2026-10-12 - graph export and Bloom visualization

Operators can now see the graph Ontos built: neutral export formats
(GraphML, Cypher, JSON-LD, SVG), a direct push to a separate Neo4j
visualization database with a ready-made Bloom perspective, and
snapshot serving from those text formats. Exports are ACL-filtered by
default and every export attempt writes an Article-12 record. The CLI
also gains an invocation-wide `--storage <url>` override.

### Upgrade notes
- **Custom `GraphStore` implementations must add two methods:**
  `scan(...)` (backend-neutral export snapshot, ACL-filtered at the
  store boundary) and `entities_for_source(...)` (write-path lookup used
  by ingest). The bundled NetworkX and Neo4j stores implement both.
- The default dev audit DB URL is now
  `sqlite+aiosqlite:///./audit-logs/audit.sqlite` (was
  `sqlite+pysqlite://…`, which the async emitter rejected).
  `aiosqlite` moved from the `dev` extra to a runtime dependency, and
  `sqlalchemy` is now `sqlalchemy[asyncio]` (see Fixed).
- Neo4j stores get a new `fact_source_idx` relationship index, created
  idempotently on startup.

### Added — graph export + Neo4j Bloom (#32, #56)
- `ontos export {neo4j,graphml,svg,json-ld}`. Entities map to nodes and
  facts to edges, keeping `t_valid` / `t_invalid` / `superseded_by` /
  `acl_ref` / confidence / source and extractor provenance plus the
  ontology id/version. Resolver history is exported as `MERGED_WITH`.
- `ontos export neo4j` writes idempotent `MERGE`-based Cypher
  (`cypher.txt` by default). `--push bolt://… --user … --password …
  --database …` writes straight to a **separate** visualization
  database, with an index per label and labels / relationship types
  derived from the ontology and sanitized. The push refuses to target
  the operational Ontos database.
- Views: `--as-of`, `--include-closed`, `--entity ID --hops N`.
- **ACL-aware:** deny-by-default; `--acting-on-behalf-of` resolves the
  caller's `view` grants before the scan. `--include-acl-mask` exports
  across ACL boundaries with stamps preserved and requires the new
  `audit_exporter` relation (`ontos.authz.AUDIT_EXPORTER`;
  `InMemoryAuthz(audit_roles=…)`).
- **Article-12 on every export:** ordinary exports, privileged
  exports (`audit_exporter=allow`) and denied privileged attempts
  (`deny`, zero facts recorded) each emit an audit record.
- `ontos serve --snapshot` now loads `.jsonld`, `.graphml` and Cypher
  (`.cypher` / `.txt`) snapshots in addition to `.pkl`. The text formats
  are the safe choice for untrusted snapshots, since pickle is not.
- `scripts/bloom-perspective.json`: Bloom perspective for the starter,
  fintech and pharma ontologies (27 node categories, 46 relationship
  types, `canonical_name` → `id` captions, `predicate` edge captions,
  saved views for public / ACL-scoped / active / closed facts).
  Setup and the operational vs visualization schema split are in
  `docs/visualization.md`.

### Added — `--storage <url>` CLI override (#37, #57)
- Top-level `ontos --storage <url>` sets graph storage for one
  invocation without touching env vars: `networkx://`,
  `networkx:///path.pkl`, `neo4j://host:port/db`, `neo4j+ssc://…`,
  `bolt+s://…`. `--storage-password-env VAR` reads the Neo4j password
  from an env var. Embedding it in the URL works but is discouraged.
- Applies to `serve`, `ingest`, `query` and `export` (#62).
  `--storage` with `--storage-path` is rejected; `serve --snapshot`
  rejects live-storage options.
- Neo4j connection settings now live on `RuntimeConfig`, and
  `build_store()` reads them from there instead of `os.environ`.
  Passwords are redacted from config reprs, CLI errors, logs and
  Article-12 payloads.

### Changed — ingest and extraction
- Re-ingesting the same source reuses its previous raw entity ids when
  `(type, canonical_name)` matches exactly one prior entity, so
  repeated ingests don't create duplicate nodes. An unsafe id
  collision skips the document under `skip_and_log` and raises under
  `fail_fast`.
- `LlmExtractor` strips type decoration (`Person:Alice`,
  `Alice (Person)`) from `canonical_name` and rejects a response that
  maps one raw id to two different identities.
- The extraction prompt now includes relation descriptions and stricter
  direction and naming rules. This can shift extraction output compared
  with 0.5.0.
- Supersession: an exact same-source re-assertion of a fact is a no-op
  for every cardinality (previously only singular relations).

### Fixed
- **Audit emitter on Apple Silicon (#58, #59):** `sqlalchemy` only
  installed `greenlet` for some `platform_machine` values, excluding
  macOS `arm64`, so every async audit write (and therefore every
  `ontos export`) failed there. Now depends on `sqlalchemy[asyncio]`.
  CI gains a `macos-14` (arm64) unit + compliance job.
- **`as_of` without a UTC offset (#60, #61)** (e.g. `2026-06-01`) is
  now interpreted as UTC across `ontos export --as-of` and the MCP
  `search` / `traverse` / `explain` tools. Previously it crashed the
  NetworkX store, matched nothing on Neo4j, and on the MCP tools failed
  before the audit record was written.
- **`--as-of` exports (#60, #61)** no longer include merge records
  resolved after the requested time.
- **`ontos --storage … export` (#62, #63)** used to silently ignore
  `--storage` and export the default dev store. The `--push` guard
  now checks the resolved config (env or `--storage`) and compares
  `host:port`, so `neo4j://` vs `bolt://` can't get past it.

## [0.5.0] - 2026-10-07 - sovereignty signals and offline operation

Three deliverables that tighten the sovereign posture (BRIDGE
guardrails on all cloud LLM backends), unlock offline review
(snapshot MCP server over a frozen graph), and fix a real
Article-12 correctness bug under concurrency (Postgres audit chain
forks). CHANGELOG keeps `[Unreleased]` empty for the next cycle.

### Added — Anthropic (Claude) LLM backend + sovereignty guardrails
- `AnthropicBackend` in `ontos/llm/anthropic.py`, behind a new
  `[anthropic]` extra. Implements both `LLMBackend` (extraction) and
  `LLMPlannerBackend` (planning) with Claude structured outputs
  (`messages.parse`), using the same Pydantic response models as
  `OpenAIBackend` — both adapters accept and reject identical output.
  Default model `claude-opus-5`.
- Server-side refusal fallbacks enabled by default (`fallbacks=False`
  opts out). A refusal from the whole chain raises a typed
  `AnthropicRefusalError`; `max_tokens` truncation raises with the
  limit in the message. Both surface through `LlmExtractor` /
  `LlmPlanner` as `ExtractionError` / `PlannerError`.
- `LLMUsage` (`ontos/llm/usage.py`): `AnthropicBackend.usage`
  accumulates input / output / cache tokens per instance, with
  `snapshot()` and subtraction for per-run deltas. Protocols unchanged.
- `ontos ingest` / `ontos query` gain `--llm-backend
  [ollama|anthropic]` (default `ollama` — no behavior change),
  `--anthropic-model`, and `--anthropic-api-key-env` (default
  `ANTHROPIC_API_KEY`). Missing key or unknown backend exits 2 with
  an actionable message. Anthropic runs print token usage to stderr.
- **BRIDGE sovereignty guardrails** (new, `ontos/llm/_sovereignty.py`):
  every BRIDGE backend construction emits a loud structlog
  `bridge-backend-instantiated` warning so operators of in-VPC
  deploys see the posture break in logs. In `ONTOS_ENV=prod` the
  backend refuses to instantiate — `SovereigntyError` — unless
  `ONTOS_ALLOW_BRIDGE_BACKENDS=1` is also set (opt-in disclosed in
  the error message). Mirrors `ontos.runtime.server`'s dev-signing-
  key default-refuse pattern.
- `OpenAIBackend` retroactively marked BRIDGE too — same instantiation
  warning and the same prod-env refusal. The `openai` pyproject
  extra description carries the posture. Pre-this-change the
  OpenAIBackend ran silently in prod environments; parity with
  Anthropic closes that gap.

### Changed — shared structured-output models
- `_ExtractionResponse`, `_PlanResponse`, and `_reify_plan` moved
  from `ontos/llm/openai.py` to `ontos/llm/_structured.py`.
  `ontos.llm.openai` re-exports the models, so existing imports keep
  working.

### Added — `ontos serve --snapshot` MCP server over a frozen graph (#33)
- New CLI flag that boots a read-only MCP server bound to a frozen
  `NetworkxStore` pickle snapshot. All existing read tools (`search`,
  `traverse`, `explain`, `provenance`, `audit_lookup`, `ask`) work
  unchanged against the snapshot; a new `snapshot_info` tool reports
  a content-hash id, the file's mtime, format, and ontology stamp
  so orchestrators can disambiguate across sources. Deliberately
  does NOT report `fact_count` (unfiltered total would leak forbidden-
  fact existence across ACLs) or the absolute path (leaks server
  filesystem layout).
- `snapshot_info` emits an Article-12 audit record on every call,
  threading through the same `_emit` closure the other MCP tools use —
  it's registered inside `build_server(snapshot_metadata=...)`, not
  at the CLI shim layer.
- The loaded store's `_path` is set to `None` after load; the
  "frozen" guarantee is structural (`flush()` early-returns) rather
  than left to the convention that today's six tools don't write.
- Honors bitemporal `as_of`, ACL filtering, and the Article-12 audit
  chain exactly as the live server does.
- Dispatches on file extension: `.pkl` / `.pickle` loads today's
  `ONTOS-NX-STORE-V2` format. JSON-LD / GraphML / Cypher paths will
  plug in once #32 lands (dispatch wired, loaders deferred).
- Mutually exclusive with `--storage-path` — the snapshot serves a
  frozen file, `--storage-path` binds the live dev store.
- WARNING: today's snapshot format is a Python pickle; the loader's
  magic-header check is a FORMAT check only (rejects non-Ontos files),
  NOT a safety mitigation — a crafted pickle with the right prefix
  still runs. Only load snapshots from trusted sources until #32's
  text formats land. A one-time structlog warning
  (`snapshot-pickle-loaded`) fires at load.

### Fixed — PostgresAuditEmitter chain forks under concurrency
- `emit_async` read the latest `prev_hash` and inserted the new row
  without any lock, so two concurrent emitters could chain off the
  same parent — after which `verify_chain_async()` reported the log
  as tampered. Writers now take a transaction-scoped Postgres
  advisory lock keyed on the audit table before reading `prev_hash`.
  SQLite is unaffected (it serializes writers already).

### Added — PostgresAuditEmitter `schema=`
- `PostgresAuditEmitter(engine, key, schema="...")` and
  `from_url(..., schema=...)` place the audit table in a named
  Postgres schema, so several independent hash chains (e.g. one per
  tenant) can share one database. Default (`None`) is unchanged.
- `count_async` uses a SQLAlchemy `count()` instead of raw SQL, so it
  respects the schema.

## [0.4.0] - 2026-10-06 - storage audit maturity

Four storage-layer invariants shipped together: Entity.type persists
across restart, resolver merges carry an audit edge, cross-ACL
entity writes fail loud instead of leaking attrs, and every fact
carries the ontology version it was extracted under. Unblocks #32
(exports) and #33 (snapshot MCP) — both can now interpret facts
across schema evolutions.

### Added — Ontology-version stamp on every fact + lazy migration (#34)
- `Provenance` gains `ontology_id` and `ontology_version` fields;
  both default to `""` for back-compat so pre-#34 Fact pickles,
  pre-#34 Neo4j edges, and 40+ existing test fixtures stay valid
  without edits. `LlmExtractor` populates both from the active
  `Ontology` on every extracted Fact and Entity.
- `Ontology` gains a top-level `id` field (default `"ontos.unknown"`;
  `docs/ontology/examples/starter.yaml` carries `ontos.starter`).
- New `ontos.migration` package: `OntologyMigration` Protocol
  (mirrors `SupersessionPolicy`'s shape), `MigrationRegistry` with
  BFS version-hop resolution + strict mode + multi-migrations-per-edge
  bundling, and three built-ins — `PredicateRename`,
  `PredicateDeprecate`, `CardinalityTighten`.
- `DeterministicExecutor` accepts an optional `current_ontology` +
  `migration_registry`; facts fetched during `_expand` are
  translated through the registry before PPR / path-flow scoring.
  Stored facts stay bitemporally immutable — only the read-side
  projection is translated. Migration events land in
  `ExecutionResult.warnings` for the Article-12 audit trail.
- `Neo4jStore` persists `prov_ontology_id` + `prov_ontology_version`
  as plain RELATES edge props (no index needed — these aren't query
  predicates). Pre-#34 edges with no ontology props still load:
  `_edge_props_to_fact` defaults both to `""`, which the registry
  treats as "unstamped" (pass-through with warning under non-strict,
  drop under strict).
- Unblocks #32 (exports can carry the schema identity in their
  manifest) and #33 (snapshot MCP manifest can stamp the schema a
  snapshot represents).

### Deferred (follow-up PR)
- `--strict-ontology` server/CLI flag plumbing. The registry
  supports strict mode programmatically today; wiring it through
  the CLI/server construction path is a separate concern.

### Fixed — cross-ACL attribute leak on upsert_entity (#48)
- `GraphStore.upsert_entity` now rejects writes that would change an
  entity's `acl_ref` to a different non-None value (or to None, which
  would unmask a restricted entity). Pre-#48 the ACL stayed on the
  first write but attrs got overwritten by any subsequent ingest
  regardless of scope — a Finance-scoped caller could read HR-sourced
  attrs under the Finance ACL. New `CrossAclUpsertError(StorageError)`
  is raised; `IngestPipeline.run()` routes it through the existing
  `ErrorPolicy` the same way it already routes `ExtractionError`
  (whole-doc skip under `SKIP_AND_LOG` preserves the "every fact
  subject/object has a backing typed node" invariant).
- Neo4j: the pre-write check runs inside a `session.execute_write`
  transaction so a rejected cross-ACL upsert rolls back atomically —
  no partial state lands.
- A `structlog.warning("cross-acl-upsert-rejected", entity_id=...,
  stored_acl=..., attempted_acl=..., attempted_canonical_name=...)`
  event fires at the raise site so compliance operators can audit
  attempted boundary crossings without parsing exception traces.
- Downstream: #46's `merges_for_entity(allowed_acls=...)` is now
  consistently safe — the "stored ACL but different-scope attrs"
  state that could silently leak through the merge-visibility
  pre-filter cannot occur. ACL and attrs can never drift apart.
- New semantics table: `None → None`, `None → X`, `X → X` proceed
  with last-write-wins on attrs; `X → None` and `X → Y` raise.
- Credit: @ALEKS0805 caught this while prepping #32's exporter.

### Added — MergeRecord persistence (#46)
- `GraphStore.record_merge(record)` + `merges_for_entity(entity_id)`
  Protocol methods. Idempotent on `(canonical_id, merged_id,
  resolver_id)`; last-write-wins on `resolved_at`. Mirrors #38's
  `upsert_entity` pattern.
- `IngestPipeline.run()` now calls `record_merge` for every merge
  the resolver cascade emits, between the resolver call and the
  fact-write loop. New `IngestReport.merges_recorded: int` counter.
- `NetworkxStore` pickle format bumped `ONTOS-NX-STORE-V1` →
  `ONTOS-NX-STORE-V2`. Load-time back-compat for V1 files
  (missing `_merges` defaults to empty); logs a structlog
  `networkx-store-migrated-from-legacy-format` info line once per
  load. Existing dev-store pickles upgrade transparently on the
  next ingest/flush.
- `Neo4jStore`: new `[:MERGED_WITH]` edge type (merged → canonical)
  with `resolver_id`, `resolver_version`, `resolved_at`, `reason`
  as edge properties. New `merge_record_idx` on
  `(resolver_id, resolved_at)` for audit queries.
- Self-loops skipped at write time — `ExactMatchResolver` includes
  the canonical in `merged_ids` by convention, but the audit edge
  only shows real consolidations.
- Pre-#46, every `MergeRecord` the resolver emitted was dropped on
  the floor after a single-run merge-count tally — a quiet
  Article-12 gap for entity-consolidation audits. Unblocks #32's
  `MERGED_WITH` edge export surface.

### Added — Entity.type persistence (#38)
- `GraphStore.upsert_entity` Protocol method — idempotent entity
  write with MERGE semantics. Accepts optional `acl_ref` so entities
  extracted from restricted documents inherit the doc's ACL on
  first write. Implemented in both `NetworkxStore` and `Neo4jStore`;
  both fire a `structlog.warning` ("entity-type-overwrite") when
  the type changes between writes so operators can audit the drift
  against the resolver's `MergeRecord`.
- `IngestPipeline.run()` now calls `upsert_entity` for every
  extracted entity before the fact-write loop, threading
  `doc.acl_ref` through. Pre-#38, extracted `Entity` objects were
  seen by the resolver (for merge counts) and then dropped on the
  floor — the resulting graph had untyped nodes with no provenance
  pointer. Unblocks #32 (Neo4j Bloom + exports) and sets the pattern
  #34 (ontology-version stamp) will follow.
- Neo4j: new `entity_type_idx` on `(:Entity).type` so #32's
  type-filtered exports stay O(matching-nodes) on 10k+ node
  graphs. Lazy migration — existing untyped nodes get `type` on
  the next ingest that touches them; no destructive one-shot.
- `Entity.properties` on Neo4j are stored as a **JSON-encoded
  string** (not a map) because Neo4j node properties can only be
  primitives or arrays of primitives. Readers decode via
  `json.loads(n.properties)`.
- `NetworkxStore` nodes now carry `type`, `canonical_name`,
  `aliases`, `properties`, `acl_ref`, `provenance_source_id`,
  `provenance_extractor_id` attrs. Pickle format unchanged
  (`nx.MultiDiGraph` serializes node attrs natively) — no
  `ONTOS-NX-STORE-V1` magic bump; existing dev-store pickles
  stay readable.
- **ACL semantics are first-write-wins.** An entity first stamped
  by a restricted doc stays restricted even when a later public
  ingest re-upserts it — prevents a silent downgrade that would
  expose the entity's name via #32's exporter. Operators who need
  to broaden an entity's ACL use a dedicated admin path, not
  routine ingest.
- **Read-side ACL contract** documented on the Protocol: backends
  that persist entity attrs MUST filter at read time on `acl_ref`
  the same way `search` / `traverse` / `facts_for_entity` already
  do for facts. Returning an entity's attrs to a caller who can't
  see any fact citing it would leak the entity's existence — the
  permission-aware-traversal invariant forbids that.

### Changed — Protocol surface
- `GraphStore` Protocol gains one method (`upsert_entity`).
  Additive, but any third-party store implementation (we ship
  `NetworkxStore` + `Neo4jStore`) will need to adopt it. No
  existing CI tests fail — no test implements `GraphStore`
  outside the shipped backends.

## [0.3.0] - 2026-10-06 - UX unblockers

Four UX fixes surfaced by real-world testing after 0.2.0: PDF
ingest (sovereign + BRIDGE paths), CLI state persistence between
`ingest` and `query`, actionable timeouts on Ollama, and the
schema-strict error contract parallel to the planner's. First
release where "quick start just works" on both `pip install
enclave-ontos` + an `ingest; query` pair AND real-world PDFs.

### Added
- **`LlamaParsePdfConnector` (BRIDGE)** — ingest PDFs via LlamaCloud's
  hosted vision API. Handles both text-based and scanned/image PDFs
  that pure-text parsers cannot read. `pip install 'enclave-ontos[llama]'`
  pulls the dep; `ontos ingest --source-dir corpus/ --pdf-backend llamaparse`
  routes PDFs through the connector. First connector to lift the
  `.txt`/`.md` restriction. Closes #28.
- **`MultiConnector`** — composes multiple sub-connectors behind one
  Connector-shaped seam. The CLI uses it to fan a mixed-type directory
  (`.txt` + `.md` + `.pdf`) into the right per-type sub-connectors in
  a single `ontos ingest` invocation. Pattern mirrors `CascadeResolver`.
- **`ontos ingest --pdf-backend`** CLI flag (choices: `llamaparse`;
  future-additive). Required when the source directory contains `.pdf`
  files; omit for pure `.txt`/`.md` ingest. Fail-loud if PDFs are
  present but the flag is unset — the pre-0.3.0 silent skip was the
  #28 UX bug.
- **`ontos ingest --llama-api-key-env`** CLI flag (default:
  `LLAMA_CLOUD_API_KEY`) — names the env var holding the LlamaCloud
  key. Keeps the key out of command-line / audit records.
- First-use `structlog.warning` emitted whenever
  `LlamaParsePdfConnector` is constructed: *"sends PDF bytes to
  api.cloud.llamaindex.ai — BRIDGE connector, not sovereignty-safe."*
  One line per process so compliance reviewers see it in `kubectl logs`.

### Changed
- `llama` extra now pulls `llama-cloud-services>=0.2` instead of the
  obsolete `llama-index` + `llama-index-graph-stores-neo4j` pins
  (dead weight from an abandoned PropertyGraphIndex design).

### Fixed
- `#30` — `httpx.ReadTimeout` from the Ollama backend no longer
  escapes as a raw stack trace to the CLI. The operator sees a
  clean `ExtractionError: Ollama did not respond within 60.0s
  (http://localhost:11434/api/chat). Pass --ollama-timeout
  <seconds> to raise it, warm the model first (ollama run
  <model>), or try a shorter document.` This was the UX issue
  that broke the #28 real-PDF smoke test.
- `LlmExtractor.extract` now wraps unexpected backend exceptions
  as `ExtractionError` (parallels `LlmPlanner.plan`'s existing
  wrap). Pre-#30, any non-`ExtractionError` from
  `structured_extract` propagated raw.
- `#29` — CLI default `NetworkxStore` no longer loses facts between
  `ontos ingest` and `ontos query` invocations. Facts now persist to
  `~/.ontos/dev-store.pkl` by default; override via `--storage-path
  PATH` or `ONTOS_STORAGE_PATH`. Pass `--storage-path ""` for explicit
  in-memory (ephemeral) mode. Operators who stay ephemeral see a loud
  stderr warning naming the data-loss consequence. Format is
  Python-pickle, dev-only — regulated deploys still use the Neo4j
  backend.

### Added — sovereign PDF ingest (#39)
- **`PypdfConnector`** — in-VPC, text-only PDF ingest via local
  `pypdf`. Zero network calls. Closes the sovereignty gap left
  open by #28's `LlamaParsePdfConnector` (BRIDGE); the pair makes
  the operator's compliance posture an explicit choice.
- **`pdf` extra** pulling `pypdf>=4.0`. Install with
  `pip install 'enclave-ontos[pdf]'`.
- **`ontos ingest --pdf-backend pypdf`** CLI dispatch alongside
  the existing `llamaparse` option. Fails loud on 0-char output
  (image-based PDF) with the exact message *"PypdfConnector got
  zero characters from X. The PDF is likely image-based; use
  --pdf-backend llamaparse for vision-based parsing."* — points
  operators at the complementary BRIDGE connector when their
  content actually needs it.
- `--pdf-backend` help text + the "PDFs found but --pdf-backend
  not set" error now list both choices with their sovereignty
  posture called out.

### Added — Ollama timeout UX (#30)
- `OllamaTimeoutError` typed exception in `ontos.llm` — the backend
  raises it when a request exceeds the configured `timeout_s`.
  Translated into `ExtractionError` / `PlannerError` by the extractor
  / planner so the operator sees an actionable message naming the
  `--ollama-timeout` knob instead of a raw httpx stack trace.
- `ontos ingest --ollama-timeout SECONDS` and `ontos query
  --ollama-timeout SECONDS` (default 60). Long docs or cold-start
  model loads may need 180–300.

### Added — storage persistence (#29)
- `NetworkxStore(path=...)` — optional persistence to a local pickle
  file. Load on construct, atomic save on `close()` (write to `.tmp`
  then `replace`), magic-header + schema version
  (`b"ONTOS-NX-STORE-V1\n"`) so a format change fails loud instead of
  silently resetting state. Zero-arg `NetworkxStore()` still means
  in-memory — no breaking change for the 55+ existing call sites.
- `StorageError` on `ontos.storage.base` — raised when a store cannot
  load, persist, or recover state (missing magic header, corrupt
  payload, unrecognised payload shape). Reusable by future
  file-backed backends.
- `--storage-path PATH` CLI flag on `ontos ingest`, `ontos query`,
  `ontos serve`.
- `ONTOS_STORAGE_PATH` env var on `RuntimeConfig`.
- `ontos ingest` now prints `facts persisted to <path>` after a
  successful run so operators see where their data went.

### Known caveats
- `LlamaParsePdfConnector` is BRIDGE-only — sends PDF bytes to
  `api.cloud.llamaindex.ai`. #39 tracks the sovereign `pypdf` in-VPC
  path for text-based PDFs (0.3.x follow-up).
- No native OCR path yet — scanned PDFs require LlamaParse (BRIDGE).
  A local Tesseract-based path is a potential follow-up if operators
  ask.

### Numbers
- **384 unit + compliance tests pass** (up from 281 at 0.2.0).
  Breakdown by PR: #40 +33 (PDF ingest), #41 +31 (persistence),
  #42 +9 (Ollama timeout), #43 +18 (sovereign pypdf).
- `ruff` + `ruff format --check` + `mypy --strict ontos` all clean.

## [0.2.0] - 2026-10-01 - correctness and vertical content

Four externally-reported bug fixes, the fintech + pharma vertical
ontologies with sample corpora and graded questions, and the
compliance posture now hardened against bitemporal drift and
permission-aware traversal edge cases.

### Added
- **Fintech + pharma vertical ontologies** — `docs/ontology/examples/fintech.yaml`
  (SEC 10-K structure + SR 11-7 model-risk vocabulary) and
  `docs/ontology/examples/pharma.yaml` (ClinicalTrials.gov +
  FDA submission ladder + ICH E6 GCP + GxP). ~15-16 entities,
  22-25 relations, 22-25 patterns each; cardinality tuned from
  real corporate / clinical reality.
- **10 synthetic sample documents** under `docs/ontology/examples/fintech-corpus/`
  and `pharma-corpus/` — all carry a `SYNTHETIC DEMONSTRATION DOCUMENT`
  marker so a swap for a real filing fails CI.
- **40 graded natural-language questions** (20 per vertical) with
  expected multi-hop path shapes, expected source documents, and
  expected answer entities. Static-validation harness pins every
  hop against the ontology and every entity against the corpus.
- **`SupersessionPolicy` Protocol + `CardinalitySupersessionPolicy`
  default** — closes conflicting active facts at ingest when the
  ontology's cardinality says the relation allows at most one object
  per subject. Same-ACL scope only. Idempotent on same-source
  re-ingest; preserves second-source corroboration.
- **`IngestReport.facts_superseded`, `facts_reaffirmed`,
  `facts_historical`, `supersession_records`** — audit visibility
  for every supersession decision the pipeline made.
- **Backfill handling** — new fact whose `t_valid` precedes an
  existing active fact is written pre-closed with `t_invalid`
  pointing at the newer active fact; the current fact stays active.
- **Direction-aware graph traversal** — executor now honors
  `TraversalStep.direction` (`in` / `out` / `both`); both
  NetworkxStore and Neo4jStore implement all three. Inverse queries
  (`Who works at Acme?`) work for the first time.
- **Seed-id resolution fallback chain** on `SeedByEntity` —
  exact match → type-suffix strip (`"Alice (Person)"` → `"Alice"`)
  → keyword search with ambiguity guard. Every resolution emits
  an operator-visible structlog warning.
- **Plan canonicalization** — `Plan.build()` drops
  consecutive-duplicate `TraversalStep`s with first-occurrence
  order preserved. Non-adjacent repeats pass through unchanged.
- **CI/CD polish** — Claude PR review action, PR review sandbox
  isolated from the review credential, PR comment dedup.

### Fixed
- `#16` — planner-emitted `(Type)` suffix on `SeedByEntity` no longer
  returns zero hits. Executor resolves via type-suffix strip or
  keyword search; refuses to guess on ambiguous substring matches.
- `#17` — inverse queries (`Who works at X?`) now return results.
  `TraversalStep.direction="in"` and `"both"` are honored by the
  executor and both store backends.
- `#18` — LLM planner's non-deterministic duplicate
  `TraversalStep`s are canonicalized at `Plan.build()`. Audit
  records show the canonical plan the executor actually ran.
- `#21` — conflicting facts at ingest now trigger supersession.
  `David Lee works_at Beta Systems` followed by `David Lee works_at
  Acme Corp` closes the Beta fact (`t_invalid` set, `superseded_by`
  pointing at Acme) instead of leaving both active. Bitemporal
  `as_of` queries return the correct historical answer.

### Compliance
- New gate-blocking suite under `tests/compliance/`:
  `test_seed_resolution_acl_leak.py`,
  `test_supersession_compliance.py`,
  `test_traversal_direction_security.py`. All three cover ACL
  isolation, bitemporal correctness, and the invariants a regulated
  buyer evaluates against during due diligence.
- `IngestPipeline` now requires `ontology`; default supersession is
  active unless the operator explicitly passes `NullSupersessionPolicy()`.
  Compliance-first default; no silent opt-out.

### Changed — API surface
- `IngestPipeline.__init__` now requires `ontology: Ontology`
  (was absent) and accepts optional `supersession: SupersessionPolicy`.
- `SeedByEntity.entity_id` is now resolved by the executor; direct
  consumers of `plan.steps` are unaffected.
- `TraversalStep.direction` is now consumed by the executor
  (previously ignored, effectively always `"out"`).

### Known caveats carried from 0.1.0 (still true in 0.2.0)
- `OpenAIBackend` is BRIDGE-only — violates sovereignty. Use
  `OllamaBackend` for regulated deploys.
- `SlackConnector` is a placeholder.
- Entity resolution ships the Rules tier only.
- No Prometheus / OTel emission.
- Single-arch amd64 image.

### Known 0.2.0 limitations (tracked for 0.3.0)
- `#28` CLI does not accept PDF files — `_read_source_dir` filters
  to `.txt / .md`.
- `#29` CLI default `storage_backend=networkx` loses facts between
  `ontos ingest` and `ontos query` invocations.
- `#30` No CLI flag to tune the Ollama timeout; cold-start on large
  docs can hit `httpx.ReadTimeout`.

### Numbers
- 281 unit + compliance tests pass (up from 183).
- 47 integration tests pass against real Neo4j 5.24 via
  testcontainers.
- `ruff` + `ruff format --check` + `mypy --strict ontos` all clean.

## [0.1.0] - 2026-09-26 - first public release

First tagged release. Encompasses milestones M0 → M5 from the design plan.
The runtime works end-to-end: ingest a directory of text files via
`ontos ingest`, query it via `ontos query "..."` or the `ask` MCP tool,
verify the audit chain via `ontos audit verify`.

### Added
- **M0 scaffold** — FastMCP server; immutable bitemporal `Fact` model;
  hash-chained in-memory Article-12 audit emitter; permission-aware
  traversal in the NetworkX dev store; Docker + docker-compose scaffold.
- **M1 storage + extraction** — `Extractor` Protocol as the Scribe-drop-in
  boundary; `Neo4jStore` with bitemporal + permission-aware Cypher;
  ontology loader (YAML) with referential integrity + cardinality;
  `LlmExtractor` (schema-strict, provenance-first, loud-on-empty).
- **M2 authz + persistent audit** — OpenFGA-shaped `AuthzBackend` with
  in-memory + OpenFGA implementations; `PostgresAuditEmitter` with
  hash-chained rows + `verify_chain_async` + retention pruning.
- **M3 planner/executor split** — typed `Plan` model validated against
  ontology; `DeterministicExecutor` with PPR (HippoRAG-style) +
  PathRAG-style flow pruning; new `ask` MCP tool that captures the
  Plan JSON in the Article-12 audit for reproducibility.
- **M3.d benchmark harness** — non-gate regression-tracking harness
  reporting recall@k, precision@k, latency percentiles.
- **M4 fabric completion** — `ExactMatchResolver` (Rules tier) +
  `CascadeResolver` chaining tiers with merge lineage preserved;
  `Connector` Protocol + `TextConnector` + Slack placeholder; Helm
  chart with deny-egress NetworkPolicy + KMS-fed Secrets + read-only
  root FS.
- **M5 usability** — `IngestPipeline` stitching Connector → Extractor →
  Resolver → Store; `OllamaBackend` (sovereignty default) and
  `OpenAIBackend` (bridge) satisfying both `LLMBackend` and
  `LLMPlannerBackend`; `ontos` CLI (`serve`, `ingest`, `query`,
  `audit verify`).
- **CI + release plumbing** — `ruff` + `mypy --strict` + `pytest` gate
  every PR; `helm lint` + `helm template` gate the chart on every PR;
  this release cuts through PyPI trusted publishing + GHCR image via
  the new release workflow.

### Known caveats
- `OpenAIBackend` is BRIDGE-only — sends prompts to `api.openai.com`
  and violates the in-VPC sovereignty promise. Use `OllamaBackend`
  (local) for regulated deploys. Enclave Scribe will replace both once
  its benchmarks pass current frontier models.
- `SlackConnector` is a placeholder; real Slack lands with per-tenant
  onboarding.
- Entity resolution ships the Rules tier only; ML + LLM tiers plug in
  through the same `Resolver` Protocol when we have real corpora at
  the >1M-record scale.
- No dashboards / Prometheus / OTel emission yet.

## [0.0.1] - 2026-09-25 - pre-alpha scaffold

Initial repository scaffold — never published; served as the M0 baseline
before the milestone plan started shipping.

- FastMCP server with `search / traverse / explain / provenance / audit_lookup` tools
- Immutable, bitemporal `Fact` model
- Hash-chained in-memory Article-12 audit emitter
- Permission-aware traversal in the NetworkX dev store
- Docker + docker-compose scaffold
