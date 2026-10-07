"""#33 — `ontos serve --snapshot` CLI tests.

Isolate the shim's error-handling (mutually-exclusive flags, extension
dispatch, missing-file / bad-magic rejection) and `_collect_snapshot_metadata`'s
shape. End-to-end MCP exercises live in
`tests/unit/test_snapshot_serve_in_memory.py`.
"""

from __future__ import annotations

import asyncio
import pickle
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from ontos.cli import app
from ontos.cli.main import _collect_snapshot_metadata, _compute_snapshot_id
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.runtime.server import SnapshotMetadata
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
    # pickle-trust caveat before they ever load a file. The review
    # (PR #53) called out that the magic-header check is a FORMAT
    # check, not a safety mitigation — --help must reflect that.
    assert "pickle" in result.stdout.lower()
    assert "trusted" in result.stdout.lower()
    assert "format check" in result.stdout.lower()


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


def test_snapshot_rejects_bad_magic_header_raises_storage_error(tmp_path: Path) -> None:
    """A `.pkl` file that isn't an ONTOS-NX-STORE-V2 payload must fail
    loudly before pickle.loads runs. The review (PR #53) called out the
    original brittle assertion — `isinstance(result.exception, StorageError)`
    alone is the right form."""
    bogus = tmp_path / "bogus.pkl"
    bogus.write_bytes(b"not-an-ontos-store\n" + pickle.dumps({}))
    result = runner.invoke(app, ["serve", "--snapshot", str(bogus)])
    assert result.exit_code != 0
    assert isinstance(result.exception, StorageError)


def test_snapshot_metadata_is_captured_at_load_time(tmp_path: Path) -> None:
    """`_collect_snapshot_metadata` produces a frozen `SnapshotMetadata`
    with the ontology stamp from the first fact and a content-derived
    id — no fact_count (would leak forbidden counts) and no absolute
    path (would leak server filesystem layout)."""
    snap = tmp_path / "snap.pkl"
    _write_snapshot_pickle(snap, with_fact=True)
    store = NetworkxStore(path=snap)

    metadata = _collect_snapshot_metadata(snap, store)
    assert isinstance(metadata, SnapshotMetadata)
    assert metadata.ontology_id == "ontos.starter"
    assert metadata.ontology_version == "0.1"
    assert metadata.format == "ontos-nx-store-v2"
    # id is a stable 16-hex-char digest of the file bytes.
    assert len(metadata.id) == 16
    assert metadata.id == _compute_snapshot_id(snap)
    # mtime parses as ISO-8601.
    datetime.fromisoformat(metadata.mtime)
    # Review BLOCKING #2 + SUGGESTION #4: metadata exposes no
    # fact_count and no filesystem path.
    serialized = metadata.model_dump(mode="json")
    assert "fact_count" not in serialized
    assert "snapshot_path" not in serialized
    assert "path" not in serialized


def test_snapshot_metadata_on_empty_snapshot_leaves_stamp_empty(tmp_path: Path) -> None:
    """A snapshot with zero facts is a valid edge case (operator
    freezes an empty store for reproducibility). Ontology stamp
    fields come back empty; no raise."""
    snap = tmp_path / "empty.pkl"
    _write_snapshot_pickle(snap, with_fact=False)
    store = NetworkxStore(path=snap)
    metadata = _collect_snapshot_metadata(snap, store)
    assert isinstance(metadata, SnapshotMetadata)
    assert metadata.ontology_id == ""
    assert metadata.ontology_version == ""


def test_snapshot_id_changes_when_file_bytes_change(tmp_path: Path) -> None:
    """A re-exported snapshot gets a new id by design, so orchestrators
    can tell versions apart without consulting mtime."""
    snap_a = tmp_path / "a.pkl"
    snap_b = tmp_path / "b.pkl"
    _write_snapshot_pickle(snap_a, with_fact=True)
    _write_snapshot_pickle(snap_b, with_fact=False)
    assert _compute_snapshot_id(snap_a) != _compute_snapshot_id(snap_b)
