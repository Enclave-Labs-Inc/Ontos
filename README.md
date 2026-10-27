# ontos

[![CI](https://github.com/Enclave-Labs-Inc/Ontos/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Enclave-Labs-Inc/Ontos/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/enclave-ontos.svg?label=PyPI&color=blue)](https://pypi.org/project/enclave-ontos/)
[![Python](https://img.shields.io/pypi/pyversions/enclave-ontos.svg?label=python)](https://pypi.org/project/enclave-ontos/)
[![Downloads](https://img.shields.io/pypi/dm/enclave-ontos.svg?label=PyPI%20downloads)](https://pypi.org/project/enclave-ontos/)
[![Container](https://img.shields.io/badge/ghcr.io-enclave--labs--inc%2Fontos-blue?logo=docker&logoColor=white)](https://github.com/Enclave-Labs-Inc/Ontos/pkgs/container/ontos)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://www.apache.org/licenses/LICENSE-2.0)

**A subset of [Enclave](https://getenclave.ai) — the knowledge-graph layer of Enclave's sovereign AI company brain.**

## What Ontos is

Ontos is an in-VPC knowledge-graph runtime. It:

1. **Extracts** typed entities and typed relationships from an enterprise's data (documents, communications, systems of record).
2. **Persists** them as immutable, bitemporally-valid, provenance-tagged facts.
3. **Serves** them to LLM agents through a Model Context Protocol (MCP) interface, with permission-aware graph traversal.
4. **Emits** a compliance-grade audit record for every query.

Ontos is the "relationships" half of Enclave's company brain. Where the retrieval substrate answers *what a document says*, Ontos answers *how things connect* — who owns what, what depends on what, what changed when, and what the source-of-truth was on a given date.

Every fact carries provenance. Every query is audit-ready. Every deployment runs inside the customer's VPC.

## Where Ontos sits in the Enclave family

| Component | Role |
|---|---|
| **enclave-runtime** | Retrieval substrate — document/passage retrieval inside the customer's VPC. |
| **ontos** *(this repo)* | Knowledge-graph layer — typed entities and relationships with provenance, bitemporal validity, and permission-aware traversal. |
| **enclave-scribe** | Sovereign extraction model (in development). Replaces the current LlamaIndex/LangChain extractors in `ontos/extraction/` once it passes benchmarks against current frontier models. |
| **enclave-ocr** | Document and image OCR for the ingestion path. |
| enclave-gtm, enclave-home, enclave-business, … | Product surfaces that consume Ontos through MCP. |

## Status

**0.7.0.** The runtime works end-to-end:
- **Ingest** text and PDF corpora (`ontos ingest`).
- **Query** with `ontos query "..."` or the `ask` MCP tool.
- **Export** the graph for visualization (`ontos export`).
- **Verify** the audit chain (`ontos audit verify`).

0.7.0 adds:
- **MCP caller authentication**, using JWTs from your own IdP.
- **Opt-in retention and audited inspection of relationships the ontology rejected.**
- **Permission checks on every lookup-by-id tool.**

It also contains **breaking changes**. Read the upgrade notes in [CHANGELOG.md](CHANGELOG.md) before upgrading. Per-milestone architecture notes live in [docs/design/](docs/design/).

## Install

```bash
# From PyPI
pip install enclave-ontos

# Or via uv
uv add enclave-ontos
```

The PyPI distribution name is `enclave-ontos`; the import name stays `ontos` (same shape as `pip install PyYAML` → `import yaml`).

Container image on GHCR (multi-platform amd64):

```bash
docker pull ghcr.io/enclave-labs-inc/ontos:0.7.0
# or the moving tag
docker pull ghcr.io/enclave-labs-inc/ontos:latest
```

## Quickstart

```bash
# From an installed release
ontos serve

# From a clone (dev)
uv sync --extra dev
uv run ontos serve
```

Then point any MCP-speaking client (Claude Code, Cursor, Codex, Gemini CLI) at the streamable-HTTP endpoint printed on start. By default the server binds `127.0.0.1` with caller authentication off. That's fine on a developer machine. Anything reachable from elsewhere needs [authentication](#securing-the-mcp-server-070).

The CLI also ships:
- `ontos ingest`
- `ontos query "<question>"`
- `ontos export {neo4j,graphml,svg,json-ld}`
- `ontos ontology candidates list`
- `ontos audit verify`

See [CHANGELOG.md](CHANGELOG.md) for what's in each release and [RELEASING.md](RELEASING.md) for the release process.

### Securing the MCP server (0.7.0+)

`ONTOS_AUTH_MODE` decides where caller identity comes from.

**`none` (the default)** trusts the `agent_identity` / `acting_on_behalf_of` strings in tool arguments. The server therefore **refuses to bind a non-loopback address** in this mode unless you set `ONTOS_ALLOW_UNAUTHENTICATED_BIND=1`. Use that opt-in for local Docker only.

**`jwt`** verifies a bearer token from your own identity provider on every request:

```bash
export ONTOS_AUTH_MODE=jwt
export ONTOS_AUTH_ISSUER=https://idp.example.internal/realms/acme
export ONTOS_AUTH_AUDIENCE=ontos
# Exactly one key source: a pinned PEM public key (no network calls)…
export ONTOS_AUTH_PUBLIC_KEY_FILE=/etc/ontos/auth/public-key.pem
# …or your IdP's JWKS endpoint (must be https)
# export ONTOS_AUTH_JWKS_URI=https://idp.example.internal/realms/acme/protocol/openid-connect/certs
ontos serve --host 0.0.0.0
```

- **Identity comes from the token.** The token's `sub` is the principal used for authorization. The acting agent is the RFC 8693 `act.sub` claim, or the client id if there isn't one.
- **A caller can't read as someone else.** An `acting_on_behalf_of` argument that disagrees with the token is rejected. To act for a user, an agent gets a delegated token from the IdP.
- **Every request is audited.** Each Article-12 record states how the caller was identified, and rejected requests are audited too.

The Helm chart requires these settings, or an explicit opt-out, before it renders. See [docs/architecture.md](docs/architecture.md#caller-authentication) for details.

### PDF ingest (0.3.0+)

`ontos ingest` reads `.txt`, `.md`, and `.pdf` files from a directory. PDFs need an explicit backend. The sovereign default choice is local `pypdf`:

```bash
pip install 'enclave-ontos[pdf]'
ontos ingest \
    --source-dir ./corpus \
    --ontology ./starter.yaml \
    --pdf-backend pypdf
```

Two backends ship:

- **`--pdf-backend pypdf`** (install `[pdf]`) — sovereign in-VPC parsing via local `pypdf`. Zero network calls. Works on PDFs with an embedded text layer (typical office / finance / legal filings). Pick this when sovereignty matters.
- **`--pdf-backend llamaparse`** (install `[llama]`) — BRIDGE, document bytes leave the customer VPC on their way to `api.cloud.llamaindex.ai`. Handles scanned / image-based PDFs via LlamaCloud's hosted vision API. Pick this only when the content actually needs vision — your `pypdf` run tells you so by failing with `"PypdfConnector got zero characters from X. The PDF is likely image-based; use --pdf-backend llamaparse ..."`.

Pure `.txt`/`.md` ingest requires no flag and no extra.

### Anthropic (Claude) backend

Ollama is the default, in-VPC LLM. To extract and plan with Claude instead:

```bash
pip install 'enclave-ontos[anthropic]'
export ANTHROPIC_API_KEY=sk-ant-...
ontos ingest --source-dir ./corpus --ontology ./starter.yaml --llm-backend anthropic
ontos query "Who works at Acme?" --ontology ./starter.yaml --llm-backend anthropic
```

`--anthropic-model` picks the model (default `claude-opus-5`). This is a BRIDGE backend: prompts and document text go to api.anthropic.com, so don't use it for in-VPC regulated deployments.

### Ollama timeouts (0.3.0+)

First-time model loads can take 30–60s. If `ontos ingest` or `ontos query` times out, warm the model via `ollama run <model>` first, or raise the per-request timeout with `--ollama-timeout 180` (or higher for long documents).

### Storage selection and dev-store persistence (0.3.0+)

By default the CLI persists dev-store facts to `~/.ontos/dev-store.pkl`, so `ontos ingest ...` followed by `ontos query ...` works out of the box without an external database. The existing `--storage-path PATH` and `ONTOS_STORAGE_PATH` options remain supported; pass `--storage-path ""` for ephemeral in-memory storage.

For an invocation-wide override, pass `--storage` **before** the subcommand:

```bash
# Ephemeral in-memory NetworkX store
ontos --storage networkx:// query "Who works at Acme?" --ontology ./starter.yaml

# Persisted NetworkX dev store
ontos --storage networkx:///tmp/scratch.pkl ingest --source-dir ./corpus --ontology ./starter.yaml

# Neo4j
ontos --storage neo4j://db.internal:7687/neo4j query "Who works at Acme?" --ontology ./starter.yaml
```

Supported storage URLs are:

- `networkx://` — ephemeral in-memory dev store.
- `networkx:///path/to/file.pkl` — persisted NetworkX dev store.
- `neo4j://host:port/database` — Neo4j.
- `neo4j+ssc://host:port/database` and `bolt+s://host:port/database` — Neo4j TLS schemes.

CLI storage settings win over storage environment settings; values omitted from a Neo4j URL, such as the username or password, are filled from `NEO4J_USER` / `NEO4J_PASSWORD`. Credentials embedded in the URL are supported for compatibility but discouraged because URLs can leak through shell history or process listings. Prefer a dedicated password environment variable:

```bash
export ONTOS_NEO4J_PASSWORD='...'
ontos \
  --storage neo4j://neo4j@db.internal:7687/neo4j \
  --storage-password-env ONTOS_NEO4J_PASSWORD \
  query "Who works at Acme?" --ontology ./starter.yaml
```

`--storage` and command-level `--storage-path` are mutually exclusive. Production deployments may continue using `ONTOS_STORAGE_BACKEND=neo4j` plus the existing `NEO4J_*` environment variables; the CLI flag is an in-process override and does not mutate those environment settings. The NetworkX pickle remains Python-version-specific, single-process, and is not a wire format.

### Snapshot MCP (0.5.0+)

Serve the MCP tool surface over a frozen graph export instead of the live store:

```bash
ontos serve --snapshot ~/snapshots/graph-2026-10-07.pkl --port 7788
```

Boots a read-only MCP server bound to the pickle. All existing read tools (`search`, `traverse`, `explain`, `provenance`, `audit_lookup`, `ask`) work unchanged; a new `snapshot_info` tool reports a content-hash id, the file's mtime, format, and the ontology stamp from #34 so orchestrators can disambiguate across sources. Mutually exclusive with `--storage-path`. Honors `as_of`, ACL filtering, and the Article-12 audit chain exactly as live-serve does — `snapshot_info` is itself audited, and intentionally reports no fact count or filesystem path.

Snapshots can be JSON-LD (`.jsonld`), GraphML (`.graphml`), Cypher (`.cypher` / `.txt`) or `ONTOS-NX-STORE` Python pickles (`.pkl` / `.pickle`, current header `V3`). Prefer the text formats.

- **Pickles run code on load.** The loader's magic-header check only rejects non-Ontos files. It is not a safety mitigation: a crafted pickle with the right prefix still runs on load, so load pickles only from trusted sources.
- **V3 dev stores can contain retained rejected candidates.** Never share a raw dev-store pickle as a snapshot.

### Graph export and Bloom (0.6.0+)

`ontos export {graphml,svg,json-ld}` writes a permission-filtered snapshot of the graph. `ontos export neo4j` writes Cypher, or with `--push`, loads it into a **separate** Neo4j visualization database with a ready-made Bloom perspective. Every export writes an Article-12 record.

The push refuses the operational database, including aliases of its host such as `localhost` vs `127.0.0.1` and differently cased database names. See [docs/visualization.md](docs/visualization.md).

### Rejected-candidate retention (0.7.0+)

Ontos only writes relationships that match a pattern in the ontology. With `ontos ingest --retain-rejected-candidates` (off by default), relationships the ontology rejects are kept in a separate candidate store, never in the trusted graph, so reviewers can see what the ontology is missing.

```bash
ontos ontology candidates list --acting-on-behalf-of user:reviewer --limit 50
```

- **Two permissions are required:** the `ontology_reviewer` relation, and VIEW on each candidate's source document.
- **Every attempt is audited**, including denials and failures.
- **Inspection is CLI-only**, run by a trusted local operator.
- **Candidates are sensitive.** They hold raw content from restricted documents and there's no automatic expiry yet.

See [docs/ontology-candidate-authz.md](docs/ontology-candidate-authz.md).

## Design pillars

1. **Every fact carries provenance** — `(source_id, extractor_version, confidence, t_valid, t_invalid)` on every triple.
2. **Every query carries an audit record** — EU AI Act Article 12: ≥12 fields per AI-influenced decision, ≥6 mo retention, per-user attribution.
3. **Permission-aware traversal at the executor** — paths crossing forbidden nodes pruned during traversal, not after. Zero node-existence leakage.
4. **Bitemporal correctness** — `as_of` on every read; contradictions close old validity windows.
5. **Planner/executor split** — LLM writes typed plans over the ontology; deterministic executor runs multi-hop retrieval.
6. **Sovereign by architecture** — nothing leaves the customer VPC. Deployable air-gapped.

## MCP tools

Every tool takes `agent_identity`, and most take `acting_on_behalf_of`. With `ONTOS_AUTH_MODE=jwt` both come from the verified token. Every call writes an Article-12 audit record.

- `search(query, as_of?, k?)`: retrieval over facts the caller may see.
- `traverse(start, relation?, depth?, as_of?)`: permission-aware multi-hop traversal.
- `explain(entity_id, as_of?)`: entity dossier with sources and validity ranges.
- `provenance(fact_id)`: full provenance for one fact. A fact outside the caller's ACLs returns `null`, the same as a missing one.
- `audit_lookup(query_id)`: the Article-12 record for one of the caller's own queries. Reading other principals' records requires `audit_exporter` on `audit`.
- `ask(question)`: natural-language question, run through the planner and deterministic executor. Needs a configured planner.
- `snapshot_info()`: metadata of the frozen snapshot, under `serve --snapshot` only.

`as_of` on the read tools gives historical answers for regulatory review.

## Layout

```
ontos/
├── runtime/      # FastMCP server, tool surface, caller auth, candidate inspection
├── cli/          # `ontos` command line
├── planner/      # NL → typed plan over the ontology
├── executor/     # deterministic multi-hop execution, authz-aware traversal
├── extraction/   # extractor interface + LLM extractor; enclave-scribe drops in here
├── llm/          # Ollama (in-VPC) and BRIDGE (Anthropic, OpenAI) backends
├── ontology/     # YAML ontology loader and validation
├── migration/    # read-side ontology version migration
├── pipeline/     # ingest pipeline: extract → resolve → supersede → write
├── resolver/     # entity resolution
├── supersession/ # bitemporal supersession policies
├── storage/      # GraphStore + CandidateStore: NetworkX (dev), Neo4j
├── exporters/    # GraphML / JSON-LD / SVG / Cypher, Neo4j push
├── authz/        # in-memory and OpenFGA authorization backends
├── audit/        # Article-12 audit emitters (in-memory, Postgres/SQLite)
└── ingest/       # source connectors (text, pypdf, LlamaParse)
```

## Contributing

Ontos is open-source and we actively want outside contributors. Start with:

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, coding standards, the non-negotiable invariants, and the PR process.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability (**do not open a public issue for security bugs**).
- GitHub Issues for bugs and feature proposals; GitHub Discussions for questions and design conversations.

## License

Apache-2.0.
