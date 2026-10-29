"""Tier 0: stores with schema setup (Neo4j indexes, uniqueness constraints,
candidate migration) must be initialized on the runtime path, not only in
tests."""

from __future__ import annotations

from fastmcp import Client

from ontos.audit.emitter import AuditEmitter
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.server import build_server, initialize_store
from ontos.storage.networkx_store import NetworkxStore


class _InitTrackingStore(NetworkxStore):
    def __init__(self) -> None:
        super().__init__()
        self.initialized = 0

    async def initialize(self) -> None:
        self.initialized += 1


async def test_initialize_store_calls_backend_setup() -> None:
    store = _InitTrackingStore()
    await initialize_store(store)
    assert store.initialized == 1


async def test_initialize_store_is_a_noop_for_stores_without_setup() -> None:
    await initialize_store(NetworkxStore())


async def test_server_lifespan_initializes_store_before_tools_run() -> None:
    store = _InitTrackingStore()
    server = build_server(
        RuntimeConfig.from_env(),
        store=store,
        audit=AuditEmitter(signing_key=b"init-test"),
    )

    async with Client(server) as client:
        assert store.initialized >= 1
        await client.call_tool("search", {"query": "x", "agent_identity": "a"})


def test_ingest_initializes_store_before_pipeline_writes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`ontos ingest` is the write path that needs Neo4j's uniqueness
    constraints; the store must be initialized before the pipeline runs."""
    from pathlib import Path

    from typer.testing import CliRunner

    import ontos.extraction
    import ontos.llm
    import ontos.pipeline
    import ontos.runtime.server
    from ontos.cli import app

    store = _InitTrackingStore()
    seen: dict[str, int] = {}

    class StubLLM:
        id = "stub-llm"

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class StubExtractor:
        def __init__(self, llm: object, ontology: object) -> None:
            pass

    class RecordingPipeline:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self):  # type: ignore[no-untyped-def]
            from ontos.pipeline.ingest import IngestReport

            seen["initialized_before_run"] = store.initialized
            return IngestReport()

    monkeypatch.setattr(ontos.llm, "OllamaBackend", StubLLM)
    monkeypatch.setattr(ontos.extraction, "LlmExtractor", StubExtractor)
    monkeypatch.setattr(ontos.pipeline, "IngestPipeline", RecordingPipeline)
    monkeypatch.setattr(ontos.runtime.server, "build_store", lambda cfg: store)

    source_dir = tmp_path / "corpus"
    source_dir.mkdir()
    (source_dir / "doc.txt").write_text("Alice works at Acme.")
    starter = (
        Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
    )

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "--source-dir",
            str(source_dir),
            "--ontology",
            str(starter),
            "--storage-path",
            str(tmp_path / "graph.pkl"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["initialized_before_run"] == 1
