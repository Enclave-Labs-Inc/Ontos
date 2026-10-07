"""#33 — `ontos serve --snapshot` CLI tests.

Isolate the shim's error-handling (mutually-exclusive flags, extension
dispatch, missing-file / bad-magic rejection) and the `snapshot_info`
tool's shape. Does not boot the fastmcp HTTP server — that path is
exercised in `tests/integration/test_snapshot_serve.py`.
"""

from __future__ import annotations

import asyncio
import pickle
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from ontos.cli import app
from ontos.cli.main import _register_snapshot_info
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.base import StorageError
from ontos.storage.networkx_store import NetworkxStore

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


def _write_snapshot_pickle(path: Path, *, with_fact: bool = True) -> None:
    """Build a NetworkxStore, add one fact under ontos.starter v0.1
    (optional), and flush to `path`. Writes a valid V2 pickle so the
    snapshot loader has something real to open."""

    async def _build() -> None:
        store = NetworkxStore(path=path)
        if with_fact:
            now = datetime.now(UTC)
            await store.add_fact(
                Fact(
                    subject_id="person:alice",
                    predicate="knows",
                    object_id="person:bob",
                    provenance=Provenance(
                        source_id="demo",
                        extractor_id="demo",
                        extractor_version="0",
                        ontology_id="ontos.starter",
                        ontology_version="0.1",
                        confidence=Confidence.EXTRACTED,
                        confidence_score=1.0,
                    ),
                    t_valid=now,
                    ingested_at=now,
                )
            )
        # force=True so an empty store still produces a file; the
        # dirty-flag gate on flush() otherwise skips writes from
        # a never-mutated store.
        store.flush(force=True)
        await store.close()

    asyncio.run(_build())


def test_serve_help_documents_snapshot_flag() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--snapshot" in result.stdout
    # Warning text must be visible in --help so operators see the
    # pickle-trust caveat before they ever load a file.
    assert "pickle" in result.stdout.lower()
    assert "trusted" in result.stdout.lower()


def test_snapshot_and_storage_path_are_mutually_exclusive(tmp_path: Path) -> None:
    snap = tmp_path / "snap.pkl"
    _write_snapshot_pickle(snap)
    result = runner.invoke(
        app, ["serve", "--snapshot", str(snap), "--storage-path", str(tmp_path / "live.pkl")]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.stdout.lower() + result.stderr.lower()


def test_snapshot_rejects_unsupported_extension_with_pointer_to_issue_32(
    tmp_path: Path,
) -> None:
    bogus = tmp_path / "snap.jsonld"
    bogus.write_text("{}")
    result = runner.invoke(app, ["serve", "--snapshot", str(bogus)])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "not supported" in combined
    assert "#32" in result.stdout + result.stderr


def test_snapshot_rejects_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["serve", "--snapshot", str(tmp_path / "nope.pkl")])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "not found" in combined


def test_snapshot_rejects_bad_magic_header(tmp_path: Path) -> None:
    """A `.pkl` file that isn't an ONTOS-NX-STORE-V2 payload must fail
    loudly at load time, before any pickle.loads runs. Mirrors
    `NetworkxStore._load_from`'s existing fail-loud behavior."""
    bogus = tmp_path / "bogus.pkl"
    bogus.write_bytes(b"not-an-ontos-store\n" + pickle.dumps({}))
    result = runner.invoke(app, ["serve", "--snapshot", str(bogus)])
    assert result.exit_code != 0
    # StorageError bubbles up through typer with a non-zero exit; the
    # magic-header message surfaces for operator orientation.
    err_text = (result.stdout + result.stderr).lower()
    assert "magic" in err_text or "format" in err_text or isinstance(result.exception, StorageError)


def test_snapshot_info_tool_reports_fact_count_and_ontology_stamp(tmp_path: Path) -> None:
    """The snapshot_info tool returns the stamp from the first fact.
    Builds the tool-registration directly rather than going through
    the HTTP server (that path is in the integration tests)."""
    snap = tmp_path / "snap.pkl"
    _write_snapshot_pickle(snap, with_fact=True)
    store = NetworkxStore(path=snap)

    # Capture the registered callable without needing a real FastMCP.
    captured: dict[str, object] = {}

    class _FakeServer:
        def tool(self, *, name: str):  # noqa: ANN001 - mimics fastmcp
            def _decorator(fn):  # noqa: ANN001
                captured[name] = fn
                return fn

            return _decorator

    _register_snapshot_info(_FakeServer(), snap, store)
    info = asyncio.run(captured["snapshot_info"]())  # type: ignore[operator]

    assert info["snapshot_path"] == str(snap.resolve())
    assert info["fact_count"] == 1
    assert info["ontology_id"] == "ontos.starter"
    assert info["ontology_version"] == "0.1"
    assert info["format"] == "ontos-nx-store-v2"
    # mtime should parse as ISO-8601.
    datetime.fromisoformat(info["snapshot_mtime"])  # type: ignore[arg-type]


def test_snapshot_info_tool_on_empty_snapshot_returns_zeros_not_raises(
    tmp_path: Path,
) -> None:
    """A snapshot with zero facts is a valid edge case (operator
    freezes an empty store for reproducibility)."""
    snap = tmp_path / "empty.pkl"
    _write_snapshot_pickle(snap, with_fact=False)
    store = NetworkxStore(path=snap)

    captured: dict[str, object] = {}

    class _FakeServer:
        def tool(self, *, name: str):  # noqa: ANN001
            def _decorator(fn):  # noqa: ANN001
                captured[name] = fn
                return fn

            return _decorator

    _register_snapshot_info(_FakeServer(), snap, store)
    info = asyncio.run(captured["snapshot_info"]())  # type: ignore[operator]
    assert info["fact_count"] == 0
    assert info["ontology_id"] == ""
    assert info["ontology_version"] == ""
