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

**0.1.0 (first public release)** — the runtime works end-to-end: ingest a directory of text files via `ontos ingest`, query it via `ontos query "..."` or the `ask` MCP tool, verify the audit chain via `ontos audit verify`. See [CHANGELOG.md](CHANGELOG.md) for what shipped in this release and [docs/design/](docs/design/) for the per-milestone architecture notes.

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
docker pull ghcr.io/enclave-labs-inc/ontos:0.1.0
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

Then point any MCP-speaking client (Claude Code, Cursor, Codex, Gemini CLI) at the streamable-HTTP endpoint printed on start.

The CLI also ships `ontos ingest <dir>`, `ontos query "<question>"`, and `ontos audit verify` — see [CHANGELOG.md](CHANGELOG.md) for what's in each release and [RELEASING.md](RELEASING.md) for the release process.

### PDF ingest (0.3.0+)

`ontos ingest` reads `.txt`, `.md`, and `.pdf` files from a directory. PDFs route through LlamaCloud's hosted vision API via the `[llama]` extra:

```bash
pip install 'enclave-ontos[llama]'
export LLAMA_CLOUD_API_KEY=llx-...
ontos ingest \
    --source-dir ./corpus \
    --ontology ./starter.yaml \
    --pdf-backend llamaparse
```

Two backends ship:

- **`--pdf-backend pypdf`** (install `[pdf]`) — sovereign in-VPC parsing via local `pypdf`. Zero network calls. Works on PDFs with an embedded text layer (typical office / finance / legal filings). Pick this when sovereignty matters.
- **`--pdf-backend llamaparse`** (install `[llama]`) — BRIDGE, document bytes leave the customer VPC on their way to `api.cloud.llamaindex.ai`. Handles scanned / image-based PDFs via LlamaCloud's hosted vision API. Pick this only when the content actually needs vision — your `pypdf` run tells you so by failing with `"PypdfConnector got zero characters from X. The PDF is likely image-based; use --pdf-backend llamaparse ..."`.

Pure `.txt`/`.md` ingest requires no flag and no extra.

### Ollama timeouts (0.3.0+)

First-time model loads can take 30–60s. If `ontos ingest` or `ontos query` times out, warm the model via `ollama run <model>` first, or raise the per-request timeout with `--ollama-timeout 180` (or higher for long documents).

### Dev-store persistence (0.3.0+)

By default the CLI persists dev-store facts to `~/.ontos/dev-store.pkl`, so `ontos ingest ...` followed by `ontos query ...` works out of the box without an external database. Override the location with `--storage-path PATH` or `ONTOS_STORAGE_PATH`; pass `--storage-path ""` for ephemeral in-memory (what pre-0.3.0 did silently — operators who opt out see a loud warning). Regulated deploys continue to use `ONTOS_STORAGE_BACKEND=neo4j`; the dev-store pickle is Python-version-specific, single-process, and not a wire format.

## Design pillars

1. **Every fact carries provenance** — `(source_id, extractor_version, confidence, t_valid, t_invalid)` on every triple.
2. **Every query carries an audit record** — EU AI Act Article 12: ≥12 fields per AI-influenced decision, ≥6 mo retention, per-user attribution.
3. **Permission-aware traversal at the executor** — paths crossing forbidden nodes pruned during traversal, not after. Zero node-existence leakage.
4. **Bitemporal correctness** — `as_of` on every read; contradictions close old validity windows.
5. **Planner/executor split** — LLM writes typed plans over the ontology; deterministic executor runs multi-hop retrieval.
6. **Sovereign by architecture** — nothing leaves the customer VPC. Deployable air-gapped.

## MCP tools (v0)

- `search(query, as_of?, k?, agent_identity)` — semantic + graph retrieval
- `traverse(start, relation, depth, as_of?, agent_identity)` — permission-aware multi-hop
- `explain(entity_id, as_of?, agent_identity)` — entity dossier with sources
- `provenance(fact_id)` — full provenance chain for one fact
- `audit(query_id)` — Article-12 audit record for a prior query
- `as_of(query, timestamp, agent_identity)` — historical query for regulatory review

## Layout

```
ontos/
├── runtime/     # FastMCP server + tool surface
├── planner/     # NL → typed plan over ontology
├── executor/    # multi-hop + PPR + pruning + authz-aware traversal
├── extraction/  # LlamaIndex/LangChain wrappers today; migrates to enclave-scribe once Scribe passes benchmarks
├── ontology/    # LinkML / YAML schemas
├── storage/     # backend-agnostic (Neo4j / NetworkX / Neptune)
├── authz/       # OpenFGA / SpiceDB
├── audit/       # Article-12 audit emitter
└── ingest/      # source connectors
```

## Contributing

Ontos is open-source and we actively want outside contributors. Start with:

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, coding standards, the non-negotiable invariants, and the PR process.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability (**do not open a public issue for security bugs**).
- GitHub Issues for bugs and feature proposals; GitHub Discussions for questions and design conversations.

## License

Apache-2.0.
