# Contributing to Ontos

Thanks for looking. Ontos is an Enclave-maintained open-source project (the knowledge-graph layer of Enclave's sovereign AI company brain) and we actively want outside contributors. This document tells you the shape of a good contribution and the invariants a PR must respect to be mergeable.

If anything here contradicts what you see in the code, the code is authoritative — please open an issue and we'll fix the doc.

## Ways to contribute

- **Code** — bug fixes, new storage backends, connectors, extractors, executor optimizations.
- **Compliance mappings** — map Ontos's audit + provenance surface to a regulatory framework (HIPAA, FedRAMP, SR 11-7, ABA Rule 1.6, GDPR, etc.). See [`docs/compliance/`](docs/compliance/).
- **Benchmarks** — reproducible harnesses (LOCOMO / HotpotQA / CypherBench-shaped) under [`tests/benchmarks/`](tests/benchmarks/). Never gate CI on these; do track regressions.
- **Ontology examples** — starter YAML/LinkML schemas for a vertical (fintech, pharma, legal, defense) under [`docs/ontology/examples/`](docs/ontology/examples/).
- **Docs** — architecture, tutorials, deployment guides.
- **Bug reports** — file an issue with a minimal reproduction.
- **Security issues** — do NOT file publicly. See [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/getenclave/ontos.git
cd ontos
cp .env.example .env      # dev defaults are fine to start
uv sync --extra dev
uv run pytest             # should be 20+ tests green
uv run ontos              # boots FastMCP on 127.0.0.1:8765
```

Optional extras when working on specific layers:

```bash
uv sync --extra dev --extra neo4j      # M1 storage
uv sync --extra dev --extra llama      # LlamaIndex-based extraction
uv sync --extra dev --extra langchain  # LangChain-based extraction
uv sync --extra dev --extra authz      # OpenFGA authz
```

## Non-negotiables — a PR that violates any of these will not merge

These are the same invariants documented in [CLAUDE.md](CLAUDE.md). They exist because regulated-industry deploys depend on them:

1. **Provenance on every fact.** Every `Fact` written to the store carries a full `Provenance` record. If your code produces a fact without a valid source, extractor id, extractor version, and confidence — the fact does not get written. No exceptions, no defaults.
2. **Audit-first.** Every code path that returns data to an agent MUST emit an `AuditRecord` before returning. The audit emitter is never optional, never inside a `try/except`, never behind a feature flag.
3. **Permission-aware traversal.** Never post-filter for permissions when a pre-filter is possible. Never leak the *existence* of a forbidden node — no counts, no "hidden" markers, no error messages that reveal the shape of what was denied.
4. **Bitemporal immutability.** `Fact` is frozen. Corrections create a new fact and close the old fact's validity window via `close_fact(...)`. Never mutate a fact in place.
5. **Sovereign.** The runtime makes no outbound network calls to anything the customer didn't configure. No telemetry to Enclave-operated services from within the runtime. Third-party LLM/embedding calls are always through customer-supplied credentials; the codebase never assumes an Enclave-hosted service is reachable.
6. **Extractor interface stays swappable.** Anything in `ontos/extraction/` must keep the interface a drop-in for Enclave Scribe (the sovereign extraction model, in development). Do NOT couple callers to LlamaIndex- or LangChain-specific types. The `Provenance` wrapper is the boundary.

If your PR needs to weaken one of these — say, add telemetry, or introduce a fallback that logs a forbidden node — open an issue first to discuss. There's usually a way to accomplish the same goal without breaking the invariant.

## Coding standards

- **Python 3.12+**. Use `from __future__ import annotations` in every module.
- **Pydantic v2** for all data at API boundaries. Immutable models (`ConfigDict(frozen=True)`) wherever the domain allows.
- **Type-check strict**: `uv run mypy ontos` must pass. New public functions get types; no `Any` without a comment explaining why.
- **Lint**: `uv run ruff check ontos tests` must pass. Line length 100.
- **Comments**: only for *why* (invariants, subtle constraints, workarounds). Never for *what*. Names should carry the "what."
- **Docstrings**: public functions and classes get them; private ones only when non-obvious.
- **No broad excepts** in a request path. If you must catch, log structured and re-raise or emit an audit.

Run everything before you push:

```bash
uv run ruff check ontos tests
uv run mypy ontos
uv run pytest -q
```

## Tests

Layout under `tests/`:

- **`unit/`** — module-level tests. Fast, no I/O, no network. Every new module gets one.
- **`integration/`** — boot the FastMCP server and exercise it as a client. Marked `@pytest.mark.integration`; run with `-m integration`.
- **`compliance/`** — authz-leak tests, Article-12 field-completeness tests, provenance-chain integrity tests. **These are gate-blocking — a failure here fails CI, period.** New features that touch authz, audit, or provenance MUST land with a matching compliance test.
- **`benchmarks/`** — LOCOMO / HotpotQA / CypherBench-shaped harnesses. Track regressions; do NOT gate CI on absolute numbers.

## Pull request process

1. **Branch naming**: `feature/<slug>`, `fix/<slug>`, `docs/<slug>`, `bench/<slug>`.
2. **DCO sign-off**: every commit must be signed off (`git commit -s`). By signing off you assert the [Developer Certificate of Origin](https://developercertificate.org/) — the code is yours to contribute under this project's license.
3. **License**: contributions land under Apache-2.0 (see [LICENSE](LICENSE) once published). Inbound license = outbound license.
4. **PR template**: fill in the whole thing. If a section doesn't apply, say "n/a" and why — don't delete it.
5. **Small PRs**: prefer several small PRs over one large one. Rule of thumb: if a reviewer can't read your diff in 15 minutes, split it.
6. **Description quality**: state the *why* first; the *what* second. Link the issue.
7. **Review**: expect 2–5 business days for first review. If it's urgent (security), say so in the PR title.
8. **CI must be green** before merge — including the compliance suite. The CI workflow (`.github/workflows/ci.yml`) runs `ruff check`, `mypy` strict, and the full `pytest` suite on every PR and on every push to `main`. Concurrent runs on the same branch are cancelled so a fast rebase doesn't waste runners. If CI is red, it's on the PR author to fix — do not merge red PRs, even with "obvious" failures.

## Versioning + CHANGELOG

Ontos follows [SemVer](https://semver.org/spec/v2.0.0.html). The single source of truth for the version is `ontos/_version.py`; `pyproject.toml` reads it via hatchling. Do NOT bump the version in a feature PR — version bumps live in dedicated release PRs (see [RELEASING.md](RELEASING.md)).

Every PR that changes user-visible behavior adds a bullet under the `## [Unreleased]` section of [CHANGELOG.md](CHANGELOG.md). The release PR then moves those bullets under a new versioned heading.

## Roadmap alignment

Ontos ships against a milestone plan (M0 → M4 → v0 GA). Before starting a large piece of work, please:

- Skim the milestone breakdown in the main plan (linked from the README).
- Open a "proposal" issue if the work touches architecture (new module, new storage backend, new authz backend, new extractor family, etc.). This saves you time — we may already have a design in flight.
- Small fixes, docs, tests, and connector adapters don't need a proposal issue; just open a PR.

## Questions

- **Bugs and feature requests** → GitHub Issues.
- **Design discussions, questions, help** → GitHub Discussions.
- **Security** → see [SECURITY.md](SECURITY.md). Do not open a public issue.
- **Anything else about Enclave** → [getenclave.ai/contact](https://getenclave.ai).

## Recognition

Every merged contributor gets added to `AUTHORS` (once we publish it) and is credited in the release notes for the version their change ships in. First-time contributors get a shout-out in the release announcement.
