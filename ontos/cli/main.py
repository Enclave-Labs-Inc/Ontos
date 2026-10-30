"""`ontos` CLI — operator entry points.

Four commands:

- `ontos serve` — boot the FastMCP server (equivalent to the old
  `ontos` invocation).
- `ontos ingest` — run an IngestPipeline over a directory of text
  files against the configured store.
- `ontos query "..."` — run one NL question through the planner +
  executor path locally and print the ranked hits.
- `ontos audit verify` — walk the in-memory or Postgres audit chain
  and report tampering.

The CLI defers heavy imports (fastmcp, neo4j, planner, extractor)
until the command that needs them, so `ontos --help` stays fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

import typer

if TYPE_CHECKING:
    from ontos.ingest import Connector, MultiConnector
    from ontos.runtime.config import RuntimeConfig
    from ontos.storage.models import GraphScan


# When the operator runs the CLI without overriding storage, we want
# "ontos ingest ...; ontos query ..." to Just Work on first install.
# Facts land under the user's home dir; override via --storage-path or
# ONTOS_STORAGE_PATH; opt out with --storage-path "". Computed per-call
# (not at import time) so tests can monkeypatch Path.home().
def _default_cli_storage_path() -> Path:
    return Path.home() / ".ontos" / "dev-store.pkl"


app = typer.Typer(
    name="ontos",
    help="Ontos — the knowledge-graph layer of Enclave's sovereign AI company brain.",
    no_args_is_help=True,
)


@app.callback()
def _main(
    ctx: typer.Context,
    storage: Annotated[
        str | None,
        typer.Option(
            "--storage",
            help=(
                "Override graph storage for this invocation. "
                "Examples: networkx://, networkx:///tmp/store.pkl, "
                "neo4j://host:7687/database."
            ),
        ),
    ] = None,
    storage_password_env: Annotated[
        str | None,
        typer.Option(
            "--storage-password-env",
            help=(
                "Environment variable containing the Neo4j password for "
                "--storage. Prefer this over embedding credentials in the URL."
            ),
        ),
    ] = None,
) -> None:
    """Configure invocation-wide CLI options."""
    ctx.ensure_object(dict)
    ctx.obj["storage_url"] = storage
    ctx.obj["storage_password_env"] = storage_password_env


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option(help="Bind host; overrides env.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port; overrides env.")] = None,
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            help=(
                "Serve MCP over a frozen graph snapshot (read-only). "
                "Accepts ONTOS-NX-STORE pickle (.pkl / .pickle) "
                "JSON-LD (.jsonld), GraphML (.graphml), and Cypher "
                "(.cypher / .txt) snapshots. WARNING: pickle "
                "snapshots use Python pickle — loading an untrusted file executes "
                "arbitrary code. FORMAT check only: the magic header rejects "
                "non-Ontos files but is NOT a safety mitigation. A crafted "
                "pickle with the right prefix still runs on load. Only load "
                "trusted pickle snapshots. Pickle dev stores (V3) can also "
                "contain retained rejected candidates; share JSON-LD, GraphML "
                "or Cypher exports instead."
            ),
        ),
    ] = None,
) -> None:
    """Boot the FastMCP server on streamable-HTTP transport.

    Two modes:

    - **Live** (default) — binds the configured `GraphStore`
      (NetworkxStore under the dev path, or Neo4j via env) and
      serves the full MCP tool surface.
    - **Snapshot** (`--snapshot <path>`) — loads a frozen graph
      from `<path>` and serves the same tool surface read-only
      against it. Mutually exclusive with `--storage-path`.
    """
    cli_options = ctx.obj if isinstance(ctx.obj, dict) else {}
    storage_url = cli_options.get("storage_url")
    storage_password_env = cli_options.get("storage_password_env")

    if snapshot is not None and (storage_path is not None or storage_url is not None):
        raise typer.BadParameter(
            "--snapshot is mutually exclusive with --storage and --storage-path. "
            "A snapshot serves a frozen graph; storage options bind the live store."
        )

    if host is not None:
        os.environ["ONTOS_LISTEN_HOST"] = host
    if port is not None:
        os.environ["ONTOS_LISTEN_PORT"] = str(port)

    if snapshot is not None:
        _serve_snapshot(snapshot)
        return

    # Deferred import so `ontos --help` doesn't drag in fastmcp.
    from ontos.runtime.server import main as _serve_main

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    _warn_if_ephemeral(cfg.storage_path, cfg.storage_backend)

    _serve_main(config=cfg)


def _compute_snapshot_id(path: Path) -> str:
    """Short stable identifier derived from the snapshot's bytes.

    64 bits (first 16 hex chars of sha256) is more than enough to
    disambiguate the handful of snapshots an orchestrator might juggle
    at once. Not security-critical. A byte-level change invalidates the
    id by design, so operators see a different id after a re-export.

    NOT derived from `path.name` or the absolute path — the review
    noted the first draft returned the full filesystem path to any
    MCP caller, which leaks the server's layout and the operator's
    naming scheme.
    """
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _collect_snapshot_metadata(path: Path, store: object) -> object:
    """Build the `SnapshotMetadata` passed into `build_server`.

    Captured at LOAD time, pre-ACL, so the resulting values are the
    same bytes returned to every caller regardless of acl_subject.
    Deliberately NO fact_count — unfiltered total would leak the
    existence of forbidden facts (CLAUDE.md: no counts, no markers).
    Deliberately NO absolute path — leaks server filesystem layout.
    Ontology stamp is read here under the loader's root context, not
    at tool-call time, so no per-caller code touches the facts dict.
    """
    from datetime import UTC, datetime

    from ontos.runtime.server import SnapshotMetadata

    facts = store._facts  # type: ignore[attr-defined]
    sample = next(iter(facts.values()), None)
    ontology_id = sample.provenance.ontology_id if sample is not None else ""
    ontology_version = sample.provenance.ontology_version if sample is not None else ""
    return SnapshotMetadata(
        id=_compute_snapshot_id(path),
        mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        format=_pickle_snapshot_format(path),
        ontology_id=ontology_id,
        ontology_version=ontology_version,
    )


def _pickle_snapshot_format(path: Path) -> str:
    """Format id from the pickle's magic header, e.g. `ontos-nx-store-v3`.

    Reported as found rather than assumed: V3 files can carry retained
    rejected candidates, which operators should be able to see at a glance.
    """
    with path.open("rb") as handle:
        header = handle.readline(64).strip().decode("ascii", errors="replace")
    return header.lower() if header.startswith("ONTOS-NX-STORE-V") else "ontos-nx-store"


def _load_snapshot(path: Path) -> tuple[object, object]:
    """Load a supported frozen snapshot and its load-time metadata."""
    from datetime import UTC, datetime

    from ontos.runtime.server import SnapshotMetadata
    from ontos.storage.networkx_store import NetworkxStore

    suffix = path.suffix.lower()
    if suffix not in (".pkl", ".pickle", ".jsonld", ".graphml", ".cypher", ".txt"):
        raise typer.BadParameter(
            f"snapshot format {path.suffix!r} is not supported. "
            "Supported formats: .pkl, .pickle, .jsonld, .graphml, .cypher, .txt."
        )
    if not path.exists():
        raise typer.BadParameter(f"snapshot file not found: {path}")

    if suffix in (".pkl", ".pickle"):
        store = NetworkxStore(path=path)
        # Structural read-only: null the persistence path after load.
        store._path = None
        return store, _collect_snapshot_metadata(path, store)

    if suffix == ".jsonld":
        from ontos.exporters.jsonld import load_jsonld

        scan, manifest = load_jsonld(path)
        store = NetworkxStore.from_scan(scan)
        metadata = SnapshotMetadata(
            id=_compute_snapshot_id(path),
            mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
            format="ontos-jsonld-v1",
            ontology_id=manifest["ontologyId"],
            ontology_version=manifest["ontologyVersion"],
        )
        return store, metadata

    if suffix == ".graphml":
        from ontos.exporters.graphml import load_graphml

        scan = load_graphml(path)
        format_name = "ontos-graphml-v1"
    else:
        from ontos.exporters.cypher import load_cypher

        scan = load_cypher(path)
        format_name = "ontos-cypher-v1"

    store = NetworkxStore.from_scan(scan)
    first_fact = min(scan.facts, key=lambda item: str(item.id), default=None)
    metadata = SnapshotMetadata(
        id=_compute_snapshot_id(path),
        mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        format=format_name,
        ontology_id=(first_fact.provenance.ontology_id if first_fact is not None else ""),
        ontology_version=(first_fact.provenance.ontology_version if first_fact is not None else ""),
    )
    return store, metadata


def _serve_snapshot(path: Path) -> None:
    """Boot an MCP server over a frozen NetworkxStore snapshot.

    Dispatches on file extension: `.pkl` / `.pickle` loads an
    ONTOS-NX-STORE pickle (V1-V3); `.jsonld` loads JSON-LD; `.graphml`
    loads GraphML; `.cypher` and `.txt` load Ontos-generated Cypher snapshots.

    The loaded store's `_path` is set to `None` after load so a
    future write tool cannot silently mutate the "frozen" file —
    `NetworkxStore.flush()` early-returns on `None`, making the
    read-only guarantee structural rather than by omission (today's
    six MCP tools don't write, but nothing enforces that invariant;
    this does).
    """
    import structlog

    log = structlog.get_logger()

    if path.suffix.lower() in (".pkl", ".pickle"):
        log.warning(
            "snapshot-pickle-loaded",
            path=str(path.resolve()),
            reason=(
                "ONTOS-NX-STORE is a Python pickle format; loading a "
                "crafted file executes arbitrary code. The loader's "
                "magic-header check is a FORMAT check only (rejects "
                "non-Ontos files), NOT a safety mitigation — a crafted "
                "pickle with the right prefix still runs. Only load "
                "trusted pickle snapshots."
            ),
        )

    # Deferred imports for help-latency parity with live `serve`.
    from ontos.audit.emitter import AuditEmitter
    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import (
        _default_dev_signing_key_if_missing,
        build_server,
        run_server,
    )

    _default_dev_signing_key_if_missing()
    store, metadata = _load_snapshot(path)
    cfg = RuntimeConfig.from_env()
    audit = AuditEmitter.from_env(cfg.audit_signing_key_env)
    server = build_server(cfg, store=store, audit=audit, snapshot_metadata=metadata)  # type: ignore[arg-type]
    run_server(
        server,
        host=cfg.listen_host,
        port=cfg.listen_port,
        mode="snapshot",
        backend="networkx-snapshot",
        auth_mode=cfg.auth_mode,
        allow_unauthenticated_bind=cfg.allow_unauthenticated_bind,
        audit=audit,
    )


@app.command()
def ingest(
    ctx: typer.Context,
    source_dir: Annotated[
        Path,
        typer.Option(
            "--source-dir",
            "-s",
            help="Directory of .txt / .md / .pdf files to ingest.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    ontology_path: Annotated[
        Path,
        typer.Option(
            "--ontology",
            "-o",
            help="Path to the ontology YAML.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    ollama_model: Annotated[
        str,
        typer.Option(
            "--ollama-model",
            help="Ollama model to use for extraction (default: llama3.1:8b).",
        ),
    ] = "llama3.1:8b",
    ollama_url: Annotated[
        str,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = "http://localhost:11434",
    ollama_timeout: Annotated[
        float,
        typer.Option(
            "--ollama-timeout",
            help=(
                "Ollama request timeout in seconds (default 60). Long "
                "docs or cold-start model loads may need 180-300. First "
                "load of a model can take 30-60s; warm it beforehand "
                "via `ollama run <model>` to avoid cold-start timeouts."
            ),
        ),
    ] = 60.0,
    llm_backend: Annotated[
        str,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM for extraction. 'ollama' is sovereign (local, in-VPC). "
                "'anthropic' is BRIDGE (Claude via api.anthropic.com — prompts "
                "and document text leave the VPC; install the [anthropic] extra)."
            ),
        ),
    ] = "ollama",
    anthropic_model: Annotated[
        str,
        typer.Option("--anthropic-model", help="Claude model for --llm-backend anthropic."),
    ] = "claude-opus-5",
    anthropic_api_key_env: Annotated[
        str,
        typer.Option(
            "--anthropic-api-key-env",
            help="Env var holding the Anthropic API key.",
        ),
    ] = "ANTHROPIC_API_KEY",
    error_policy: Annotated[
        str,
        typer.Option(
            "--error-policy",
            help="fail_fast | skip_and_log",
        ),
    ] = "fail_fast",
    pdf_backend: Annotated[
        str | None,
        typer.Option(
            "--pdf-backend",
            help=(
                "PDF ingest backend. 'pypdf' is sovereign (local, text-only, "
                "zero network — install the [pdf] extra). 'llamaparse' is "
                "BRIDGE (LlamaCloud hosted vision API — handles scanned PDFs "
                "but sends document bytes off-prem; install the [llama] extra). "
                "Required when the source directory contains .pdf files; omit "
                "for pure .txt/.md ingest."
            ),
        ),
    ] = None,
    llama_api_key_env: Annotated[
        str,
        typer.Option(
            "--llama-api-key-env",
            help="Env var holding the LlamaCloud API key.",
        ),
    ] = "LLAMA_CLOUD_API_KEY",
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral "
                "in-memory (facts die with the process). Ignored for "
                "ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    retain_rejected_candidates: Annotated[
        bool,
        typer.Option(
            "--retain-rejected-candidates",
            help=(
                "Opt in to retaining rejected ontology triples in separate "
                "candidate storage. Disabled by default. No individual "
                "candidate purge or automatic TTL expiry is currently "
                "implemented; protect and manage the entire store."
            ),
        ),
    ] = False,
) -> None:
    """Ingest a directory of .txt / .md / .pdf files via an LLM extractor + the resolver cascade."""
    # Deferred imports keep --help fast.
    from ontos.extraction import LlmExtractor
    from ontos.ontology import load_ontology
    from ontos.pipeline import ErrorPolicy, IngestPipeline
    from ontos.resolver import ExactMatchResolver
    from ontos.runtime.server import build_store

    connector, document_count = _build_source_connector(
        source_dir,
        pdf_backend=pdf_backend,
        llama_api_key_env=llama_api_key_env,
    )
    typer.echo(f"discovered {document_count} document(s) in {source_dir}")

    cli_options = ctx.obj if isinstance(ctx.obj, dict) else {}
    storage_url = cli_options.get("storage_url")
    storage_password_env = cli_options.get("storage_password_env")

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    _warn_if_ephemeral(cfg.storage_path, cfg.storage_backend)

    ontology = load_ontology(ontology_path)
    llm = _build_llm(
        llm_backend,
        ollama_model=ollama_model,
        ollama_url=ollama_url,
        ollama_timeout=ollama_timeout,
        anthropic_model=anthropic_model,
        anthropic_api_key_env=anthropic_api_key_env,
    )
    extractor = LlmExtractor(llm, ontology)
    resolver = ExactMatchResolver()
    store = build_store(cfg)

    from ontos.storage.base import CandidateStore

    candidate_store: CandidateStore | None = None
    if retain_rejected_candidates:
        if not isinstance(store, CandidateStore):
            raise typer.BadParameter(
                "Configured storage backend does not support candidate retention."
            )
        candidate_store = store

    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=resolver,
        store=store,
        ontology=ontology,
        candidate_store=candidate_store,
        error_policy=ErrorPolicy(error_policy),
    )

    async def _run_pipeline() -> None:
        from ontos.runtime.server import initialize_store

        try:
            await initialize_store(store)
            report = await pipeline.run()
            typer.echo(report.model_dump_json(indent=2))
            _echo_llm_usage(llm)
        finally:
            # Explicit close so NetworkxStore(path=...) flushes the
            # pickle to disk. #29: pre-fix, process exit happened
            # before any save and new ingest/query processes started
            # from an empty graph. Flush errors are logged, not
            # re-raised — a pipeline exception, if any, must survive.
            await _close_store_without_masking(store)

    asyncio.run(_run_pipeline())
    if resolved_path is not None and cfg.storage_backend == "networkx":
        typer.secho(f"facts persisted to {resolved_path}", fg=typer.colors.GREEN)


@app.command()
def query(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="The natural-language question.")],
    ontology_path: Annotated[
        Path,
        typer.Option(
            "--ontology",
            "-o",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    ollama_model: Annotated[str, typer.Option("--ollama-model")] = "llama3.1:8b",
    ollama_url: Annotated[str, typer.Option("--ollama-url")] = "http://localhost:11434",
    ollama_timeout: Annotated[
        float,
        typer.Option(
            "--ollama-timeout",
            help=(
                "Ollama request timeout in seconds (default 60). Raise "
                "it if the planner LLM is slow on first call (model "
                "cold-start) — warm with `ollama run <model>` to avoid."
            ),
        ),
    ] = 60.0,
    llm_backend: Annotated[
        str,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM for query planning. 'ollama' is sovereign (local, in-VPC). "
                "'anthropic' is BRIDGE (Claude via api.anthropic.com — prompts "
                "and document text leave the VPC; install the [anthropic] extra)."
            ),
        ),
    ] = "ollama",
    anthropic_model: Annotated[
        str,
        typer.Option("--anthropic-model", help="Claude model for --llm-backend anthropic."),
    ] = "claude-opus-5",
    anthropic_api_key_env: Annotated[
        str,
        typer.Option(
            "--anthropic-api-key-env",
            help="Env var holding the Anthropic API key.",
        ),
    ] = "ANTHROPIC_API_KEY",
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl (same default as "
                "`ontos ingest`). Pass '' for ephemeral in-memory. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
) -> None:
    """Run one NL question through planner + executor locally; print ranked hits."""
    from ontos.executor import DeterministicExecutor
    from ontos.ontology import load_ontology
    from ontos.planner import LlmPlanner
    from ontos.runtime.server import build_store

    cli_options = ctx.obj if isinstance(ctx.obj, dict) else {}
    storage_url = cli_options.get("storage_url")
    storage_password_env = cli_options.get("storage_password_env")

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    _warn_if_ephemeral(cfg.storage_path, cfg.storage_backend)

    ontology = load_ontology(ontology_path)
    llm = _build_llm(
        llm_backend,
        ollama_model=ollama_model,
        ollama_url=ollama_url,
        ollama_timeout=ollama_timeout,
        anthropic_model=anthropic_model,
        anthropic_api_key_env=anthropic_api_key_env,
    )
    planner = LlmPlanner(llm, ontology)
    executor = DeterministicExecutor()
    store = build_store(cfg)

    async def _run() -> None:
        from ontos.runtime.server import initialize_store

        try:
            await initialize_store(store)
            plan = await planner.plan(question, ontology)
            result = await executor.execute(plan, store)
            typer.echo(
                json.dumps(
                    {
                        "plan": plan.model_dump(mode="json"),
                        "hits": [h.model_dump(mode="json") for h in result.hits],
                        "warnings": result.warnings,
                    },
                    indent=2,
                    default=str,
                )
            )
            _echo_llm_usage(llm)
        finally:
            # Query is read-only. The dirty-flag gate on
            # NetworkxStore.flush() ensures close() does NOT rewrite
            # the pickle here — important to avoid clobbering a
            # concurrent `ontos ingest` with this process's stale
            # snapshot.
            await _close_store_without_masking(store)

    asyncio.run(_run())


export_app = typer.Typer(help="Export graph snapshots for visualization.")
app.add_typer(export_app, name="export")


async def _emit_export_audit(
    *,
    db_url: str,
    signing_key_env: str,
    scan: GraphScan | None,
    acting_on_behalf_of: str | None,
    export_format: str,
    include_acl_mask: bool,
    decision: str,
    latency_ms: float,
) -> None:
    """Persist an Article-12 record for every graph export attempt."""
    from collections import Counter

    from ontos.audit.postgres import PostgresAuditEmitter

    facts = [] if scan is None else scan.facts
    fact_count_by_acl = Counter(
        fact.acl_ref if fact.acl_ref is not None else "public" for fact in facts
    )

    query_text = f"ontos export {export_format}"
    if include_acl_mask:
        query_text += " --include-acl-mask"

    policy_decisions = (
        [
            {
                "relation": "audit_exporter",
                "object": "export",
                "decision": decision,
            }
        ]
        if include_acl_mask
        else []
    )

    emitter = PostgresAuditEmitter.from_url(
        db_url,
        signing_key_env=signing_key_env,
    )

    try:
        await emitter.initialize()
        await emitter.emit_async(
            query_id=None,
            agent_identity="ontos-cli",
            acting_on_behalf_of=acting_on_behalf_of,
            query_text=query_text,
            tool_invoked=f"ontos.export.{export_format}",
            tool_arguments={
                "include_acl_mask": include_acl_mask,
                "fact_count": len(facts),
                "fact_count_by_acl": dict(sorted(fact_count_by_acl.items())),
                "export_format": export_format,
            },
            result_fact_ids=[str(fact.id) for fact in facts],
            latency_ms=latency_ms,
            model_versions={},
            policy_decisions=policy_decisions,
        )
    finally:
        await emitter.close()


def _scan_for_export(
    storage_path: str | None,
    *,
    as_of: str | None = None,
    include_closed: bool = False,
    entity: str | None = None,
    hops: int | None = None,
    acting_on_behalf_of: str | None = None,
    include_acl_mask: bool = False,
    export_format: str = "graph",
    storage_url: str | None = None,
    storage_password_env: str | None = None,
    push_target: tuple[str, str] | None = None,
) -> GraphScan:
    """Load the configured store, scan it, audit if required, and close safely.

    ``storage_url`` / ``storage_password_env`` carry the invocation-wide
    ``ontos --storage`` override. ``push_target`` is the ``(uri, database)``
    an ``export neo4j --push`` will write to; it is refused when it names
    the operational store being read.
    """
    import time

    from ontos.authz import AUDIT_EXPORTER, VIEW
    from ontos.runtime.server import (
        _default_dev_signing_key_if_missing,
        build_authz,
        build_store,
    )
    from ontos.runtime.temporal import parse_as_of

    started_at = time.perf_counter()

    try:
        as_of_timestamp = parse_as_of(as_of) if as_of is not None else None
    except ValueError as exc:
        raise typer.BadParameter("--as-of must be a valid ISO-8601 timestamp.") from exc

    if as_of_timestamp is not None and include_closed:
        raise typer.BadParameter("--as-of and --include-closed cannot be used together.")

    if include_acl_mask and acting_on_behalf_of is None:
        raise typer.BadParameter("--include-acl-mask requires --acting-on-behalf-of.")

    if entity is None and hops is not None:
        raise typer.BadParameter("--hops requires --entity.")

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    _warn_if_ephemeral(cfg.storage_path, cfg.storage_backend)
    if push_target is not None:
        _reject_operational_push_target(cfg, *push_target)
    _default_dev_signing_key_if_missing()
    authz = build_authz(cfg) if acting_on_behalf_of is not None else None
    store = build_store(cfg)

    async def _run() -> GraphScan:
        from ontos.runtime.server import initialize_store

        try:
            await initialize_store(store)
            if include_acl_mask:
                if authz is None:
                    raise typer.BadParameter(
                        "--include-acl-mask requires an authz backend granting "
                        "the audit_exporter role."
                    )

                assert acting_on_behalf_of is not None
                authorized = await authz.check(
                    acting_on_behalf_of,
                    AUDIT_EXPORTER,
                    "export",
                )
                if not authorized:
                    await _emit_export_audit(
                        db_url=cfg.audit_db_url,
                        signing_key_env=cfg.audit_signing_key_env,
                        scan=None,
                        acting_on_behalf_of=acting_on_behalf_of,
                        export_format=export_format,
                        include_acl_mask=True,
                        decision="deny",
                        latency_ms=(time.perf_counter() - started_at) * 1000,
                    )
                    raise typer.BadParameter("--include-acl-mask requires the audit_exporter role.")

            allowed_acls: list[str] | None = None
            if not include_acl_mask and authz is not None and acting_on_behalf_of is not None:
                allowed_acls = await authz.list_authorized_objects(
                    acting_on_behalf_of,
                    VIEW,
                )

            scan = await store.scan(
                as_of=as_of_timestamp,
                include_closed=include_closed,
                acl_subject=acting_on_behalf_of,
                allowed_acls=allowed_acls,
                include_all_acls=include_acl_mask,
            )

            if entity is not None:
                neighborhood_hops = 2 if hops is None else hops
                scan = _filter_export_neighborhood(
                    scan,
                    entity=entity,
                    hops=neighborhood_hops,
                )

            await _emit_export_audit(
                db_url=cfg.audit_db_url,
                signing_key_env=cfg.audit_signing_key_env,
                scan=scan,
                acting_on_behalf_of=acting_on_behalf_of,
                export_format=export_format,
                include_acl_mask=include_acl_mask,
                decision="allow",
                latency_ms=(time.perf_counter() - started_at) * 1000,
            )

            return scan
        finally:
            await _close_store_without_masking(store)

    return asyncio.run(_run())


def _cli_storage_override(ctx: typer.Context) -> tuple[str | None, str | None]:
    """Return the invocation-wide ``(--storage, --storage-password-env)`` values."""
    cli_options = ctx.obj if isinstance(ctx.obj, dict) else {}
    return cli_options.get("storage_url"), cli_options.get("storage_password_env")


_LOOPBACK = "<loopback>"


def _host_identities(host: str, port: int) -> set[str]:
    """Every name a host can be matched under: itself, its addresses, loopback.

    `localhost`, `127.0.0.1` and `::1` are one machine, and a DNS name and
    its IP are one server, so comparing raw hostnames let a push reach
    the operational database under an alias (#74).
    """
    import ipaddress
    import socket

    name = host.lower().rstrip(".")
    identities = {name}
    candidates = [name]
    # Unresolvable here (offline operator box): fall back to the literal.
    with contextlib.suppress(OSError):
        candidates += [
            str(info[4][0]) for info in socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        ]
    for candidate in candidates:
        if candidate == "localhost" or candidate.endswith(".localhost"):
            identities.add(_LOOPBACK)
            continue
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            continue
        identities.add(_LOOPBACK if address.is_loopback else str(address))
    return identities


def _neo4j_endpoint(uri: str) -> tuple[set[str], int]:
    # neo4j://, bolt://, neo4j+s:// … all reach the same server for a given
    # host:port, so the scheme must not let a push slip past the guard.
    parsed = urlsplit(uri)
    port = parsed.port or 7687
    return _host_identities(parsed.hostname or "", port), port


def _reject_operational_push_target(
    cfg: RuntimeConfig,
    push_uri: str,
    push_database: str,
) -> None:
    """Refuse an ``export neo4j --push`` aimed at the operational Ontos database.

    Applies whenever an operational Neo4j is configured, even when this
    export reads from NetworkX: a dev export must not land in the
    production database either. Database names compare case-insensitively,
    as Neo4j resolves them.
    """
    if cfg.neo4j_uri is None:
        return
    push_hosts, push_port = _neo4j_endpoint(push_uri)
    operational_hosts, operational_port = _neo4j_endpoint(cfg.neo4j_uri)
    if (
        push_port == operational_port
        and push_hosts & operational_hosts
        and push_database.casefold() == cfg.neo4j_database.casefold()
    ):
        raise typer.BadParameter(
            "--push must target a separate Neo4j visualization database, "
            "not the operational Ontos database."
        )


def _filter_export_neighborhood(
    scan: GraphScan,
    *,
    entity: str,
    hops: int,
) -> GraphScan:
    """Restrict an already permission-filtered scan to an entity neighborhood."""
    adjacency: dict[str, set[str]] = {}

    def connect(left: str, right: str) -> None:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    for fact in scan.facts:
        connect(fact.subject_id, fact.object_id)

    for record in scan.merges:
        for merged_id in record.merged_ids:
            connect(merged_id, record.canonical_id)

    visible_ids = {item.id for item in scan.entities}
    if entity not in visible_ids:
        return scan.model_copy(update={"entities": [], "facts": [], "merges": []})

    included = {entity}
    frontier = {entity}

    for _ in range(hops):
        next_frontier: set[str] = set()
        for current in frontier:
            next_frontier.update(adjacency.get(current, set()))
        next_frontier -= included
        included.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break

    entities = [item for item in scan.entities if item.id in included]
    facts = [
        fact for fact in scan.facts if fact.subject_id in included and fact.object_id in included
    ]

    merges = []
    for record in scan.merges:
        if record.canonical_id not in included:
            continue
        merged_ids = [merged_id for merged_id in record.merged_ids if merged_id in included]
        if merged_ids:
            merges.append(record.model_copy(update={"merged_ids": merged_ids}))

    return scan.model_copy(
        update={
            "entities": entities,
            "facts": facts,
            "merges": merges,
        }
    )


@export_app.command("neo4j")
def export_neo4j(
    ctx: typer.Context,
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination Cypher file.",
        ),
    ] = Path("cypher.txt"),
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Export the graph as it existed at this ISO-8601 timestamp. "
                "Timestamps without a UTC offset are interpreted as UTC."
            ),
        ),
    ] = None,
    include_closed: Annotated[
        bool,
        typer.Option(
            "--include-closed",
            help="Include closed historical facts in the export.",
        ),
    ] = False,
    entity: Annotated[
        str | None,
        typer.Option(
            "--entity",
            help="Restrict the export to this entity and its neighborhood.",
        ),
    ] = None,
    hops: Annotated[
        int | None,
        typer.Option(
            "--hops",
            min=0,
            help="Neighborhood depth for --entity. Default: 2.",
        ),
    ] = None,
    push: Annotated[
        str | None,
        typer.Option(
            "--push",
            help="Push directly to a separate Neo4j visualization database URI.",
        ),
    ] = None,
    user: Annotated[
        str,
        typer.Option(
            "--user",
            help="Neo4j username for --push.",
        ),
    ] = "neo4j",
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            help="Neo4j password for --push.",
        ),
    ] = None,
    database: Annotated[
        str,
        typer.Option(
            "--database",
            help="Neo4j visualization database for --push.",
        ),
    ] = "neo4j",
    acting_on_behalf_of: Annotated[
        str | None,
        typer.Option(
            "--acting-on-behalf-of",
            help="Caller identity used to resolve ACL permissions for export.",
        ),
    ] = None,
    include_acl_mask: Annotated[
        bool,
        typer.Option(
            "--include-acl-mask",
            help=(
                "Privileged audit export: include data across ACL boundaries "
                "with ACL stamps preserved. Requires the audit_exporter role "
                "and --acting-on-behalf-of."
            ),
        ),
    ] = False,
) -> None:
    """Export visualization-oriented Neo4j Cypher."""
    from ontos.exporters.cypher import push_neo4j, render_cypher

    if push is not None and password is None:
        raise typer.BadParameter("--password is required with --push.")

    storage_url, storage_password_env = _cli_storage_override(ctx)
    scan = _scan_for_export(
        storage_path,
        as_of=as_of,
        include_closed=include_closed,
        entity=entity,
        hops=hops,
        acting_on_behalf_of=acting_on_behalf_of,
        include_acl_mask=include_acl_mask,
        export_format="neo4j",
        storage_url=storage_url,
        storage_password_env=storage_password_env,
        push_target=(push, database) if push is not None else None,
    )

    if push is not None:
        from neo4j import AsyncGraphDatabase

        assert password is not None
        driver = AsyncGraphDatabase.driver(
            push,
            auth=(user, password),
        )

        async def _push() -> None:
            try:
                await push_neo4j(scan, driver, database=database)
            finally:
                await driver.close()

        asyncio.run(_push())
        typer.echo(f"pushed Neo4j visualization snapshot to {push} (database={database})")
        return

    output.write_text(render_cypher(scan), encoding="utf-8")
    typer.echo(f"wrote Neo4j Cypher snapshot to {output}")


@export_app.command("graphml")
def export_graphml(
    ctx: typer.Context,
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination GraphML file.",
        ),
    ] = Path("graph.graphml"),
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Export the graph as it existed at this ISO-8601 timestamp. "
                "Timestamps without a UTC offset are interpreted as UTC."
            ),
        ),
    ] = None,
    include_closed: Annotated[
        bool,
        typer.Option(
            "--include-closed",
            help="Include closed historical facts in the export.",
        ),
    ] = False,
    entity: Annotated[
        str | None,
        typer.Option(
            "--entity",
            help="Restrict the export to this entity and its neighborhood.",
        ),
    ] = None,
    hops: Annotated[
        int | None,
        typer.Option(
            "--hops",
            min=0,
            help="Neighborhood depth for --entity. Default: 2.",
        ),
    ] = None,
    acting_on_behalf_of: Annotated[
        str | None,
        typer.Option(
            "--acting-on-behalf-of",
            help="Caller identity used to resolve ACL permissions for export.",
        ),
    ] = None,
    include_acl_mask: Annotated[
        bool,
        typer.Option(
            "--include-acl-mask",
            help=(
                "Privileged audit export: include data across ACL boundaries "
                "with ACL stamps preserved. Requires the audit_exporter role "
                "and --acting-on-behalf-of."
            ),
        ),
    ] = False,
) -> None:
    """Export GraphML for graph visualization tools."""
    from ontos.exporters.graphml import render_graphml

    storage_url, storage_password_env = _cli_storage_override(ctx)

    scan = _scan_for_export(
        storage_path,
        as_of=as_of,
        include_closed=include_closed,
        entity=entity,
        hops=hops,
        acting_on_behalf_of=acting_on_behalf_of,
        include_acl_mask=include_acl_mask,
        export_format="graphml",
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    output.write_text(render_graphml(scan), encoding="utf-8")
    typer.echo(f"wrote GraphML snapshot to {output}")


@export_app.command("svg")
def export_svg(
    ctx: typer.Context,
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination SVG file.",
        ),
    ] = Path("graph.svg"),
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Export the graph as it existed at this ISO-8601 timestamp. "
                "Timestamps without a UTC offset are interpreted as UTC."
            ),
        ),
    ] = None,
    include_closed: Annotated[
        bool,
        typer.Option(
            "--include-closed",
            help="Include closed historical facts in the export.",
        ),
    ] = False,
    entity: Annotated[
        str | None,
        typer.Option(
            "--entity",
            help="Restrict the export to this entity and its neighborhood.",
        ),
    ] = None,
    hops: Annotated[
        int | None,
        typer.Option(
            "--hops",
            min=0,
            help="Neighborhood depth for --entity. Default: 2.",
        ),
    ] = None,
    acting_on_behalf_of: Annotated[
        str | None,
        typer.Option(
            "--acting-on-behalf-of",
            help="Caller identity used to resolve ACL permissions for export.",
        ),
    ] = None,
    include_acl_mask: Annotated[
        bool,
        typer.Option(
            "--include-acl-mask",
            help=(
                "Privileged audit export: include data across ACL boundaries "
                "with ACL stamps preserved. Requires the audit_exporter role "
                "and --acting-on-behalf-of."
            ),
        ),
    ] = False,
) -> None:
    """Export a static SVG graph visualization."""
    from ontos.exporters.svg import render_svg

    storage_url, storage_password_env = _cli_storage_override(ctx)

    scan = _scan_for_export(
        storage_path,
        as_of=as_of,
        include_closed=include_closed,
        entity=entity,
        hops=hops,
        acting_on_behalf_of=acting_on_behalf_of,
        include_acl_mask=include_acl_mask,
        export_format="svg",
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    output.write_text(render_svg(scan), encoding="utf-8")
    typer.echo(f"wrote SVG snapshot to {output}")


@export_app.command("json-ld")
def export_json_ld(
    ctx: typer.Context,
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination JSON-LD file.",
        ),
    ] = Path("graph.jsonld"),
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Local persistence file for the dev NetworkxStore. "
                "Default: ~/.ontos/dev-store.pkl. Pass '' for ephemeral. "
                "Ignored for ONTOS_STORAGE_BACKEND=neo4j."
            ),
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Export the graph as it existed at this ISO-8601 timestamp. "
                "Timestamps without a UTC offset are interpreted as UTC."
            ),
        ),
    ] = None,
    include_closed: Annotated[
        bool,
        typer.Option(
            "--include-closed",
            help="Include closed historical facts in the export.",
        ),
    ] = False,
    entity: Annotated[
        str | None,
        typer.Option(
            "--entity",
            help="Restrict the export to this entity and its neighborhood.",
        ),
    ] = None,
    hops: Annotated[
        int | None,
        typer.Option(
            "--hops",
            min=0,
            help="Neighborhood depth for --entity. Default: 2.",
        ),
    ] = None,
    acting_on_behalf_of: Annotated[
        str | None,
        typer.Option(
            "--acting-on-behalf-of",
            help="Caller identity used to resolve ACL permissions for export.",
        ),
    ] = None,
    include_acl_mask: Annotated[
        bool,
        typer.Option(
            "--include-acl-mask",
            help=(
                "Privileged audit export: include data across ACL boundaries "
                "with ACL stamps preserved. Requires the audit_exporter role "
                "and --acting-on-behalf-of."
            ),
        ),
    ] = False,
) -> None:
    """Export JSON-LD graph data."""
    from ontos.exporters.jsonld import render_jsonld

    storage_url, storage_password_env = _cli_storage_override(ctx)

    scan = _scan_for_export(
        storage_path,
        as_of=as_of,
        include_closed=include_closed,
        entity=entity,
        hops=hops,
        acting_on_behalf_of=acting_on_behalf_of,
        include_acl_mask=include_acl_mask,
        export_format="json-ld",
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )
    output.write_text(render_jsonld(scan), encoding="utf-8")
    typer.echo(f"wrote JSON-LD snapshot to {output}")


ontology_app = typer.Typer(
    help="Ontology administration commands.",
    no_args_is_help=True,
)
candidates_app = typer.Typer(
    help="Inspect rejected ontology candidates.",
    no_args_is_help=True,
)

app.add_typer(ontology_app, name="ontology")
ontology_app.add_typer(candidates_app, name="candidates")


@candidates_app.command("list")
def ontology_candidates_list(
    ctx: typer.Context,
    acting_on_behalf_of: Annotated[
        str,
        typer.Option(
            "--acting-on-behalf-of",
            help="Operator-asserted subject identity (not authenticated).",
        ),
    ],
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            # Range-checked by the inspection service, not Typer, so an
            # out-of-range value is audited like any other bad request.
            help="Maximum candidates to return (1-100).",
        ),
    ] = 100,
    after: Annotated[
        str | None,
        typer.Option(
            "--after",
            help="Opaque cursor from the preceding candidate page.",
        ),
    ] = None,
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help="Local NetworkX storage path, if not using --storage.",
        ),
    ] = None,
) -> None:
    """List rejected candidates in a trusted local operator context.

    M1 requires a deployment-host operator. The asserted subject is
    checked for ontology_reviewer and source-level VIEW permissions.
    Network-exposed inspection is not supported.
    """
    from ontos.audit.postgres import PostgresAuditEmitter
    from ontos.runtime.candidate_inspection import (
        CandidateInspectionDeniedError,
        CandidateInspectionRequestError,
        audit_inspection_failure,
        inspect_candidates,
    )
    from ontos.runtime.server import build_authz, build_store, initialize_store
    from ontos.storage.base import CandidateStore, InvalidCandidateCursorError

    storage_url, storage_password_env = _cli_storage_override(ctx)

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)

    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )

    if not cfg.audit_db_url:
        typer.echo("Persistent audit database is required.", err=True)
        raise typer.Exit(code=2)

    if not os.environ.get(cfg.audit_signing_key_env):
        typer.echo("Audit signing key is required.", err=True)
        raise typer.Exit(code=2)

    audit = PostgresAuditEmitter.from_url(
        cfg.audit_db_url,
        signing_key_env=cfg.audit_signing_key_env,
    )

    async def _audit_setup_failure(
        decision: Literal["authorization_error", "storage_error"],
    ) -> None:
        await audit_inspection_failure(
            audit=audit,
            subject=acting_on_behalf_of,
            agent_identity="ontos-cli-local-operator",
            decision=decision,
            limit=limit,
            after=after,
            identity_assurance="operator_asserted",
        )

    async def _run() -> None:
        import structlog

        log = structlog.get_logger()
        store = None
        try:
            try:
                await audit.initialize()
            except Exception as exc:
                # Without a working audit sink nothing may be read: fail
                # closed before touching authz or candidate storage.
                log.error("candidate-inspection-audit-unavailable", error_type=type(exc).__name__)
                typer.echo("Audit store unavailable; no candidates were read.", err=True)
                raise typer.Exit(code=1) from None

            try:
                authz = build_authz(cfg)
            except Exception as exc:
                log.error("candidate-inspection-authz-setup-failed", error_type=type(exc).__name__)
                await _audit_setup_failure("authorization_error")
                typer.echo("Candidate inspection authorization is unavailable.", err=True)
                raise typer.Exit(code=1) from None

            try:
                store = build_store(cfg)
                if not isinstance(store, CandidateStore):
                    raise TypeError("storage backend does not support candidates")
                await initialize_store(store)
            except Exception as exc:
                log.error("candidate-inspection-store-setup-failed", error_type=type(exc).__name__)
                await _audit_setup_failure("storage_error")
                typer.echo("Candidate storage is unavailable.", err=True)
                raise typer.Exit(code=1) from None

            page = await inspect_candidates(
                candidate_store=store,
                authz=authz,
                audit=audit,
                subject=acting_on_behalf_of,
                agent_identity="ontos-cli-local-operator",
                limit=limit,
                after=after,
                identity_assurance="operator_asserted",
                paginated=True,
            )

            typer.echo(
                json.dumps(
                    {
                        "candidates": [
                            candidate.model_dump(mode="json") for candidate in page.candidates
                        ],
                        "next_cursor": page.next_cursor,
                    },
                    indent=2,
                )
            )
        except typer.Exit:
            raise
        except CandidateInspectionDeniedError:
            typer.echo("Candidate inspection denied.", err=True)
            raise typer.Exit(code=1) from None
        except (CandidateInspectionRequestError, InvalidCandidateCursorError):
            typer.echo("Invalid candidate inspection request.", err=True)
            raise typer.Exit(code=2) from None
        except Exception as exc:
            # inspect_candidates audited this attempt before re-raising;
            # don't print a traceback that could carry candidate content.
            log.error("candidate-inspection-failed", error_type=type(exc).__name__)
            typer.echo("Candidate inspection failed.", err=True)
            raise typer.Exit(code=1) from None
        finally:
            if store is not None:
                await _close_store_without_masking(store)
            await _close_store_without_masking(audit)

    asyncio.run(_run())


suggestions_app = typer.Typer(
    help="Review on-demand ontology suggestions.",
    no_args_is_help=True,
)
ontology_app.add_typer(suggestions_app, name="suggestions")


@suggestions_app.command("list")
def ontology_suggestions_list(
    ctx: typer.Context,
    acting_on_behalf_of: Annotated[
        str,
        typer.Option(
            "--acting-on-behalf-of",
            help="Operator-asserted subject identity (not authenticated).",
        ),
    ],
    ontology_path: Annotated[
        Path,
        typer.Option(
            "--ontology",
            help="Current ontology YAML used to validate proposals.",
        ),
    ],
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help="Local NetworkX storage path, if not using --storage.",
        ),
    ] = None,
    llm_model: Annotated[
        str | None,
        typer.Option(
            "--llm-model",
            help="Local Ollama model for M2b suggestions; omit for deterministic M2a.",
        ),
    ] = None,
) -> None:
    """List audited, on-demand ontology suggestions for a local reviewer."""
    import structlog

    from ontos.audit.postgres import PostgresAuditEmitter
    from ontos.llm.ollama import OllamaBackend
    from ontos.ontology import load_ontology
    from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
    from ontos.runtime.ontology_suggestions import (
        audit_suggestion_setup_failure,
        list_ontology_suggestions,
    )
    from ontos.runtime.server import build_authz, build_store, initialize_store
    from ontos.storage.base import CandidateStore

    log = structlog.get_logger()
    storage_url, storage_password_env = _cli_storage_override(ctx)

    if storage_url is not None and storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    try:
        ontology = load_ontology(ontology_path)
    except Exception:
        typer.echo("Unable to load ontology.", err=True)
        raise typer.Exit(code=2) from None

    resolved_path = None if storage_url is not None else _resolve_storage_path(storage_path)

    cfg = _build_cli_runtime_config(
        resolved_path,
        storage_url=storage_url,
        storage_password_env=storage_password_env,
    )

    if not cfg.audit_db_url:
        typer.echo("Persistent audit database is required.", err=True)
        raise typer.Exit(code=2)

    if not os.environ.get(cfg.audit_signing_key_env):
        typer.echo("Audit signing key is required.", err=True)
        raise typer.Exit(code=2)

    audit = PostgresAuditEmitter.from_url(
        cfg.audit_db_url,
        signing_key_env=cfg.audit_signing_key_env,
    )

    async def _run() -> None:
        store = None
        try:
            try:
                await audit.initialize()
            except Exception as exc:
                log.error(
                    "ontology-suggestions-audit-unavailable",
                    error_type=type(exc).__name__,
                )
                typer.echo(
                    "Audit store unavailable; no candidates were read.",
                    err=True,
                )
                raise typer.Exit(code=1) from None

            try:
                authz = build_authz(cfg)
            except Exception as exc:
                log.error(
                    "ontology-suggestions-authz-setup-failed",
                    error_type=type(exc).__name__,
                )
                await audit_suggestion_setup_failure(
                    audit=audit,
                    subject=acting_on_behalf_of,
                    agent_identity="ontos-cli-local-operator",
                    ontology=ontology,
                    decision="authorization_error",
                )
                typer.echo("Suggestions authorization unavailable.", err=True)
                raise typer.Exit(code=1) from None

            try:
                store = build_store(cfg)
                if not isinstance(store, CandidateStore):
                    raise TypeError("Storage backend does not support candidates")
                await initialize_store(store)
            except Exception as exc:
                log.error(
                    "ontology-suggestions-store-setup-failed",
                    error_type=type(exc).__name__,
                )
                await audit_suggestion_setup_failure(
                    audit=audit,
                    subject=acting_on_behalf_of,
                    agent_identity="ontos-cli-local-operator",
                    ontology=ontology,
                    decision="storage_error",
                )
                typer.echo("Candidate storage unavailable.", err=True)
                raise typer.Exit(code=1) from None

            suggestions, _ = await list_ontology_suggestions(
                candidate_store=store,
                authz=authz,
                audit=audit,
                ontology=ontology,
                subject=acting_on_behalf_of,
                agent_identity="ontos-cli-local-operator",
                identity_assurance="operator_asserted",
                backend=(OllamaBackend(llm_model) if llm_model is not None else None),
            )

            typer.echo(
                json.dumps(
                    {
                        "ontology_id": ontology.id,
                        "ontology_version": ontology.version,
                        "suggestions": [
                            suggestion.model_dump(mode="json") for suggestion in suggestions
                        ],
                    },
                    indent=2,
                )
            )

        except typer.Exit:
            raise
        except CandidateInspectionDeniedError:
            typer.echo("Ontology suggestions denied.", err=True)
            raise typer.Exit(code=1) from None
        except Exception as exc:
            log.error(
                "ontology-suggestions-failed",
                error_type=type(exc).__name__,
            )
            typer.echo("Ontology suggestions failed.", err=True)
            raise typer.Exit(code=1) from None
        finally:
            if store is not None:
                await _close_store_without_masking(store)
            await _close_store_without_masking(audit)

    asyncio.run(_run())


audit_app = typer.Typer(help="Audit chain commands.")
app.add_typer(audit_app, name="audit")


@audit_app.command("verify")
def audit_verify(
    db_url: Annotated[
        str | None,
        typer.Option(
            "--db-url",
            help="Postgres audit DB URL. Defaults to ONTOS_AUDIT_DB_URL.",
        ),
    ] = None,
    signing_key_env: Annotated[
        str,
        typer.Option("--signing-key-env", help="Env var holding the HMAC signing key."),
    ] = "ONTOS_AUDIT_SIGNING_KEY",
) -> None:
    """Walk the Postgres audit chain and report tampering.

    Only the Postgres path is supported here — the in-memory emitter's
    chain lives inside a running server and is verified via the `audit`
    MCP tool, not the CLI.
    """
    from ontos.audit.postgres import PostgresAuditEmitter

    resolved_url = db_url or os.environ.get("ONTOS_AUDIT_DB_URL")
    if not resolved_url:
        typer.secho(
            "no --db-url and no ONTOS_AUDIT_DB_URL set — cannot verify.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    emitter = PostgresAuditEmitter.from_url(resolved_url, signing_key_env=signing_key_env)

    async def _run() -> None:
        ok = await emitter.verify_chain_async()
        count = await emitter.count_async()
        await emitter.close()
        if ok:
            typer.secho(
                f"audit chain verified: {count} record(s), no tampering detected.",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(
                f"audit chain BROKEN across {count} record(s) — investigate immediately.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

    asyncio.run(_run())


def _resolve_storage_path(cli_flag: str | None) -> Path | None:
    """Precedence: CLI flag → ONTOS_STORAGE_PATH env → sensible default.

    - ``cli_flag is None``: operator didn't pass ``--storage-path``.
      Fall through to env, then to the default.
    - ``cli_flag == ""``: explicit in-memory opt-out. Returns None.
    - ``cli_flag`` otherwise: use that path.

    The sensible default (``~/.ontos/dev-store.pkl``) applies ONLY when
    the backend is networkx; other backends ignore storage_path.
    """
    if cli_flag is not None:
        if cli_flag == "":
            return None
        return Path(cli_flag).expanduser().resolve()

    env_raw = os.environ.get("ONTOS_STORAGE_PATH")
    if env_raw is not None:
        return Path(env_raw).expanduser().resolve() if env_raw else None

    if os.environ.get("ONTOS_STORAGE_BACKEND", "networkx") == "networkx":
        return _default_cli_storage_path()
    return None


def _build_cli_runtime_config(
    storage_path: Path | None,
    *,
    storage_url: str | None = None,
    storage_password_env: str | None = None,
) -> RuntimeConfig:
    """Build CLI runtime config with storage-specific overrides.

    Precedence for storage settings is:

    ``--storage`` URL > environment-derived RuntimeConfig > defaults.

    The legacy ``--storage-path`` path remains supported when ``--storage``
    is absent. Supplying both is rejected rather than silently choosing one.
    """
    from ontos.runtime.config import RuntimeConfig, StorageConfig

    base = RuntimeConfig.from_env()

    if storage_url is None:
        if storage_password_env is not None:
            raise typer.BadParameter("--storage-password-env requires --storage with a Neo4j URL.")
        return dataclasses.replace(base, storage_path=storage_path)

    if storage_path is not None:
        raise typer.BadParameter("--storage and --storage-path cannot be used together.")

    try:
        storage = StorageConfig.from_url(storage_url)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --storage URL: {exc}") from exc

    if storage_password_env is not None:
        if storage.backend != "neo4j":
            raise typer.BadParameter(
                "--storage-password-env is only valid with a Neo4j --storage URL."
            )

        password = os.environ.get(storage_password_env)
        if password is None:
            raise typer.BadParameter(
                f"--storage-password-env names unset environment variable {storage_password_env!r}."
            )

        storage = dataclasses.replace(
            storage,
            neo4j_password=password,
        )

    return base.with_storage_override(storage)


async def _close_store_without_masking(store: object) -> None:
    """Call ``await store.close()`` without letting flush errors mask
    an in-flight pipeline exception.

    Pre-#41-review, `await store.close()` inside a `finally` block would
    replace the pipeline's original exception if close itself raised
    (e.g. an `OSError` from the pickle write). The pipeline exception
    is the one the operator needs to see; swallow + log the close
    failure instead.
    """
    try:
        await store.close()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — intentional, logged
        import structlog

        structlog.get_logger().error(
            "store-close-failed",
            error=repr(exc),
            message=(
                "store.close() raised; in-memory data may not have been "
                "persisted. Original pipeline result (if any) stands."
            ),
        )


def _warn_if_ephemeral(storage_path: Path | None, backend: str) -> None:
    """Loud warning for operators who opted out of persistence."""
    if backend == "networkx" and storage_path is None:
        typer.secho(
            "WARNING: storage backend is in-memory (networkx, no path). "
            "Facts written by this process will NOT be visible to future "
            "`ontos query` invocations. For persistent dev storage pass "
            "--storage-path PATH, set ONTOS_STORAGE_PATH, or configure "
            "ONTOS_STORAGE_BACKEND=neo4j for production.",
            fg=typer.colors.YELLOW,
            err=True,
        )


TEXT_EXTENSIONS = frozenset({".txt", ".md"})
PDF_EXTENSIONS = frozenset({".pdf"})


def _read_source_dir(source_dir: Path) -> dict[str, str]:
    """Load every .txt / .md file under source_dir as a document keyed on relative path."""
    docs: dict[str, str] = {}
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        rel = path.relative_to(source_dir).as_posix()
        docs[rel] = path.read_text(encoding="utf-8", errors="replace")
    return docs


def _collect_pdf_paths(source_dir: Path) -> list[Path]:
    return sorted(
        p for p in source_dir.rglob("*") if p.is_file() and p.suffix.lower() in PDF_EXTENSIONS
    )


def _build_llm(
    llm_backend: str,
    *,
    ollama_model: str,
    ollama_url: str,
    ollama_timeout: float,
    anthropic_model: str,
    anthropic_api_key_env: str,
) -> Any:
    """Construct the extraction/planning LLM, failing loud on bad config."""
    if llm_backend == "ollama":
        from ontos.llm import OllamaBackend

        return OllamaBackend(ollama_model, base_url=ollama_url, timeout_s=ollama_timeout)
    if llm_backend == "anthropic":
        api_key = os.environ.get(anthropic_api_key_env)
        if not api_key:
            typer.secho(
                f"--llm-backend anthropic requires {anthropic_api_key_env} to be set.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        from ontos.llm import AnthropicBackend

        return AnthropicBackend(model=anthropic_model, api_key=api_key)
    typer.secho(
        f"unknown --llm-backend {llm_backend!r}. Supported: ollama, anthropic.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(2)


def _echo_llm_usage(llm: Any) -> None:
    usage = getattr(llm, "usage", None)
    if usage is None or not usage.calls:
        return
    typer.echo(
        f"{llm.id}: {usage.calls} call(s), {usage.input_tokens} input tokens "
        f"({usage.cache_read_input_tokens} cached), {usage.output_tokens} output tokens",
        err=True,
    )


def _build_source_connector(
    source_dir: Path,
    *,
    pdf_backend: str | None,
    llama_api_key_env: str,
) -> tuple[MultiConnector, int]:
    """Walk source_dir, dispatch per-extension sub-connectors, wrap in MultiConnector.

    Fails loud if ``.pdf`` files are present but ``--pdf-backend`` is not set,
    rather than silently skipping them. The old behavior (silent skip) was the
    #28 UX bug the operator hit during the 0.2.0 testing week.
    """
    from ontos.ingest import (
        LlamaParsePdfConnector,
        MultiConnector,
        PypdfConnector,
        TextConnector,
    )

    sub_connectors: list[Connector] = []
    document_count = 0

    text_docs = _read_source_dir(source_dir)
    if text_docs:
        sub_connectors.append(TextConnector(text_docs))
        document_count += len(text_docs)

    pdf_paths = _collect_pdf_paths(source_dir)
    if pdf_paths:
        if pdf_backend is None:
            typer.secho(
                f"found {len(pdf_paths)} PDF file(s) in {source_dir} but "
                "--pdf-backend is not set. Pass --pdf-backend pypdf for "
                "sovereign in-VPC parsing (text PDFs only), --pdf-backend "
                "llamaparse for the LlamaCloud BRIDGE path (handles scanned "
                "PDFs), or remove the PDFs from the source directory.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)

        if pdf_backend == "pypdf":
            sub_connectors.append(PypdfConnector(pdf_paths, root=source_dir))
            document_count += len(pdf_paths)
        elif pdf_backend == "llamaparse":
            api_key = os.environ.get(llama_api_key_env, "")
            if not api_key:
                typer.secho(
                    f"--pdf-backend llamaparse requires {llama_api_key_env} "
                    "to be set in the environment.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=2)
            sub_connectors.append(
                LlamaParsePdfConnector(pdf_paths, api_key=api_key, root=source_dir)
            )
            document_count += len(pdf_paths)
        else:
            typer.secho(
                f"unknown --pdf-backend {pdf_backend!r}. Supported: "
                "'pypdf' (sovereign), 'llamaparse' (BRIDGE).",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)

    if not sub_connectors:
        typer.secho(
            f"no .txt / .md / .pdf files found under {source_dir}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    return MultiConnector(sub_connectors), document_count


if __name__ == "__main__":
    app()
