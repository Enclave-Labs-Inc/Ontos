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
import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from ontos.ingest import Connector, MultiConnector
    from ontos.runtime.config import RuntimeConfig


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


@app.command()
def serve(
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
                "Accepts an ONTOS-NX-STORE-V2 pickle today (.pkl / "
                ".pickle); JSON-LD / GraphML / Cypher paths arrive "
                "with issue #32. WARNING: today's snapshot format is "
                "a Python pickle — loading an untrusted file executes "
                "arbitrary code. The magic-header check in the loader "
                "is a FORMAT check (it rejects non-Ontos files), NOT "
                "a safety mitigation — a crafted pickle with the right "
                "prefix still runs on load. Only load snapshots from "
                "trusted sources until #32's text formats land."
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
    if snapshot is not None and storage_path is not None:
        raise typer.BadParameter(
            "--snapshot and --storage-path are mutually exclusive. "
            "The snapshot flag serves a frozen file; --storage-path "
            "binds the live dev NetworkxStore."
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

    # Thread the CLI's resolved storage path through the env so the
    # server picks it up via RuntimeConfig.from_env(). `serve` is
    # read-only against the dev store today — the MCP tools in
    # runtime/server.py (search / traverse / facts_for_entity /
    # get_fact) do not write — so no explicit flush is needed. The
    # dirty-flag gate on NetworkxStore.flush() makes this safe even
    # if a future write-capable tool lands: an unmodified store will
    # not clobber a concurrent writer's snapshot.
    resolved = _resolve_storage_path(storage_path)
    if resolved is not None:
        os.environ["ONTOS_STORAGE_PATH"] = str(resolved)
    elif storage_path == "":
        # Explicit opt-out: clear any inherited env.
        os.environ.pop("ONTOS_STORAGE_PATH", None)
    _serve_main()


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
        format="ontos-nx-store-v2",
        ontology_id=ontology_id,
        ontology_version=ontology_version,
    )


def _serve_snapshot(path: Path) -> None:
    """Boot an MCP server over a frozen NetworkxStore snapshot.

    Dispatches on file extension: `.pkl` / `.pickle` loads today's
    ONTOS-NX-STORE-V2 pickle. JSON-LD / GraphML / Cypher paths raise
    with a pointer at #32 until that work ships.

    The loaded store's `_path` is set to `None` after load so a
    future write tool cannot silently mutate the "frozen" file —
    `NetworkxStore.flush()` early-returns on `None`, making the
    read-only guarantee structural rather than by omission (today's
    six MCP tools don't write, but nothing enforces that invariant;
    this does).
    """
    import structlog

    log = structlog.get_logger()

    if path.suffix.lower() not in (".pkl", ".pickle"):
        raise typer.BadParameter(
            f"snapshot format {path.suffix!r} is not supported yet. "
            "Today's snapshot format is an ONTOS-NX-STORE-V2 pickle "
            "(.pkl / .pickle); JSON-LD / GraphML / Cypher support "
            "arrives with issue #32."
        )
    if not path.exists():
        raise typer.BadParameter(f"snapshot file not found: {path}")

    log.warning(
        "snapshot-pickle-loaded",
        path=str(path.resolve()),
        reason=(
            "ONTOS-NX-STORE-V2 is a Python pickle format; loading a "
            "crafted file executes arbitrary code. The loader's "
            "magic-header check is a FORMAT check only (rejects "
            "non-Ontos files), NOT a safety mitigation — a crafted "
            "pickle with the right prefix still runs. Only load from "
            "trusted sources until #32's text formats land."
        ),
    )

    # Deferred imports for help-latency parity with live `serve`.
    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import (
        _default_dev_signing_key_if_missing,
        build_server,
        run_server,
    )
    from ontos.storage.networkx_store import NetworkxStore

    _default_dev_signing_key_if_missing()

    store = NetworkxStore(path=path)
    # Structural read-only: null the persistence path AFTER load so
    # `NetworkxStore.flush()` early-returns if any future write tool
    # ever lands. The "frozen" guarantee is now enforced, not left
    # to the convention that today's six tools don't write.
    store._path = None

    metadata = _collect_snapshot_metadata(path, store)
    cfg = RuntimeConfig.from_env()
    server = build_server(cfg, store=store, snapshot_metadata=metadata)  # type: ignore[arg-type]
    run_server(
        server,
        host=cfg.listen_host,
        port=cfg.listen_port,
        mode="snapshot",
        backend="networkx-snapshot",
    )


@app.command()
def ingest(
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
) -> None:
    """Ingest a directory of .txt / .md / .pdf files via Ollama + the resolver cascade."""
    # Deferred imports keep --help fast.
    from ontos.extraction import LlmExtractor
    from ontos.llm import OllamaBackend
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

    resolved_path = _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(resolved_path)
    _warn_if_ephemeral(resolved_path, cfg.storage_backend)

    ontology = load_ontology(ontology_path)
    llm = OllamaBackend(ollama_model, base_url=ollama_url, timeout_s=ollama_timeout)
    extractor = LlmExtractor(llm, ontology)
    resolver = ExactMatchResolver()
    store = build_store(cfg)

    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=resolver,
        store=store,
        ontology=ontology,
        error_policy=ErrorPolicy(error_policy),
    )

    async def _run_pipeline() -> None:
        try:
            report = await pipeline.run()
            typer.echo(report.model_dump_json(indent=2))
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
    from ontos.llm import OllamaBackend
    from ontos.ontology import load_ontology
    from ontos.planner import LlmPlanner
    from ontos.runtime.server import build_store

    resolved_path = _resolve_storage_path(storage_path)
    cfg = _build_cli_runtime_config(resolved_path)
    _warn_if_ephemeral(resolved_path, cfg.storage_backend)

    ontology = load_ontology(ontology_path)
    llm = OllamaBackend(ollama_model, base_url=ollama_url, timeout_s=ollama_timeout)
    planner = LlmPlanner(llm, ontology)
    executor = DeterministicExecutor()
    store = build_store(cfg)

    async def _run() -> None:
        try:
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
        finally:
            # Query is read-only. The dirty-flag gate on
            # NetworkxStore.flush() ensures close() does NOT rewrite
            # the pickle here — important to avoid clobbering a
            # concurrent `ontos ingest` with this process's stale
            # snapshot.
            await _close_store_without_masking(store)

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


def _build_cli_runtime_config(storage_path: Path | None) -> RuntimeConfig:
    """Produce a RuntimeConfig that overrides from_env's storage_path.

    Keeps from_env() strictly env-driven so tests that call it directly
    stay in-memory; the "sensible default" layer lives in the CLI only.
    """
    from ontos.runtime.config import RuntimeConfig

    base = RuntimeConfig.from_env()
    return dataclasses.replace(base, storage_path=storage_path)


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
