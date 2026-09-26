# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every release ships:

- a matching git tag `vX.Y.Z`,
- a PyPI wheel + sdist at `ontos==X.Y.Z`,
- a GHCR container image at `ghcr.io/enclave-labs-inc/ontos:X.Y.Z`,
- a Helm chart `appVersion` bumped to match under `deploy/helm/ontos/Chart.yaml`.

See [RELEASING.md](RELEASING.md) for how a release is cut.

## [Unreleased]

Nothing yet. New work between releases lands here.

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
