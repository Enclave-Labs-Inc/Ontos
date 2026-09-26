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
import json
import os
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="ontos",
    help="Ontos — the knowledge-graph layer of Enclave's sovereign AI company brain.",
    no_args_is_help=True,
)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind host; overrides env.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port; overrides env.")] = None,
) -> None:
    """Boot the FastMCP server on streamable-HTTP transport."""
    # Deferred import so `ontos --help` doesn't drag in fastmcp.
    from ontos.runtime.server import main as _serve_main

    if host is not None:
        os.environ["ONTOS_LISTEN_HOST"] = host
    if port is not None:
        os.environ["ONTOS_LISTEN_PORT"] = str(port)
    _serve_main()


@app.command()
def ingest(
    source_dir: Annotated[
        Path,
        typer.Option(
            "--source-dir",
            "-s",
            help="Directory of .txt/.md files to ingest.",
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
    error_policy: Annotated[
        str,
        typer.Option(
            "--error-policy",
            help="fail_fast | skip_and_log",
        ),
    ] = "fail_fast",
) -> None:
    """Ingest a directory of text files via Ollama + the M4.a resolver cascade."""
    # Deferred imports keep --help fast.
    from ontos.extraction import LlmExtractor
    from ontos.ingest import TextConnector
    from ontos.llm import OllamaBackend
    from ontos.ontology import load_ontology
    from ontos.pipeline import ErrorPolicy, IngestPipeline
    from ontos.resolver import ExactMatchResolver
    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import build_store

    documents = _read_source_dir(source_dir)
    typer.echo(f"discovered {len(documents)} document(s) in {source_dir}")

    ontology = load_ontology(ontology_path)
    connector = TextConnector(documents)
    llm = OllamaBackend(ollama_model, base_url=ollama_url)
    extractor = LlmExtractor(llm, ontology)
    resolver = ExactMatchResolver()
    store = build_store(RuntimeConfig.from_env())

    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=resolver,
        store=store,
        error_policy=ErrorPolicy(error_policy),
    )
    report = asyncio.run(pipeline.run())
    typer.echo(report.model_dump_json(indent=2))


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
    ollama_model: Annotated[
        str, typer.Option("--ollama-model")
    ] = "llama3.1:8b",
    ollama_url: Annotated[
        str, typer.Option("--ollama-url")
    ] = "http://localhost:11434",
) -> None:
    """Run one NL question through planner + executor locally; print ranked hits."""
    from ontos.executor import DeterministicExecutor
    from ontos.llm import OllamaBackend
    from ontos.ontology import load_ontology
    from ontos.planner import LlmPlanner
    from ontos.runtime.config import RuntimeConfig
    from ontos.runtime.server import build_store

    ontology = load_ontology(ontology_path)
    llm = OllamaBackend(ollama_model, base_url=ollama_url)
    planner = LlmPlanner(llm, ontology)
    executor = DeterministicExecutor()
    store = build_store(RuntimeConfig.from_env())

    async def _run() -> None:
        plan = await planner.plan(question, ontology)
        result = await executor.execute(plan, store)
        typer.echo(json.dumps({
            "plan": plan.model_dump(mode="json"),
            "hits": [h.model_dump(mode="json") for h in result.hits],
            "warnings": result.warnings,
        }, indent=2, default=str))

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

    emitter = PostgresAuditEmitter.from_url(
        resolved_url, signing_key_env=signing_key_env
    )

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


def _read_source_dir(source_dir: Path) -> dict[str, str]:
    """Load every .txt / .md file under source_dir as a document keyed on relative path."""
    docs: dict[str, str] = {}
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        rel = path.relative_to(source_dir).as_posix()
        docs[rel] = path.read_text(encoding="utf-8", errors="replace")
    return docs


if __name__ == "__main__":
    app()
