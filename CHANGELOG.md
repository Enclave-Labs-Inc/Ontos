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

0.5.0 track.

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
