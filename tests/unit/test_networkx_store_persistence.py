"""NetworkxStore file-persistence tests — the #29 fix.

Verifies that passing ``path=`` to the constructor round-trips facts
through a dev-local pickle file so the two-process CLI pattern
(`ontos ingest ...; ontos query ...`) works without an external store.
"""

from __future__ import annotations

import pickle
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.base import StorageError
from ontos.storage.networkx_store import NetworkxStore


def _fact(subject: str, predicate: str, obj: str) -> Fact:
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1),
        ingested_at=datetime(2026, 1, 1),
    )


def test_zero_arg_constructor_still_in_memory() -> None:
    """Regression guard for the 55+ existing call sites."""
    store = NetworkxStore()
    assert store.path is None


def test_constructor_with_missing_path_starts_empty(tmp_path: Path) -> None:
    pkl = tmp_path / "no-such-file.pkl"
    store = NetworkxStore(path=pkl)
    assert store.path == pkl
    # File isn't created until something is persisted.
    assert not pkl.exists()


def test_constructor_with_empty_file_starts_empty(tmp_path: Path) -> None:
    pkl = tmp_path / "empty.pkl"
    pkl.touch()
    store = NetworkxStore(path=pkl)
    assert store.path == pkl


async def test_round_trip_persists_facts_across_two_store_instances(
    tmp_path: Path,
) -> None:
    """The #29 repro, inverted — now facts survive 'close + new store'."""
    pkl = tmp_path / "store.pkl"

    writer = NetworkxStore(path=pkl)
    await writer.add_fact(_fact("alice", "works_at", "acme"))
    await writer.add_fact(_fact("bob", "works_at", "acme"))
    await writer.add_fact(_fact("alice", "friend_of", "bob"))
    await writer.close()
    assert pkl.exists()
    assert pkl.stat().st_size > len(NetworkxStore._MAGIC_HEADER)

    reader = NetworkxStore(path=pkl)
    hits = await reader.facts_for_entity("alice")
    predicates = sorted(f.predicate for f in hits)
    assert predicates == ["friend_of", "works_at"]


async def test_flush_without_close_still_persists(tmp_path: Path) -> None:
    pkl = tmp_path / "flush.pkl"
    store = NetworkxStore(path=pkl)
    await store.add_fact(_fact("alice", "works_at", "acme"))
    store.flush()
    # Reading from a second instance before close means mid-session flush
    # already made the data visible.
    reader = NetworkxStore(path=pkl)
    hits = await reader.facts_for_entity("alice")
    assert len(hits) == 1


async def test_flush_is_no_op_when_path_is_none() -> None:
    store = NetworkxStore()  # no path
    await store.add_fact(_fact("alice", "works_at", "acme"))
    store.flush()  # must not crash, must not try to write anywhere


def test_load_fails_loud_on_missing_magic_header(tmp_path: Path) -> None:
    pkl = tmp_path / "junk.pkl"
    pkl.write_bytes(b"not an ontos store\n" + pickle.dumps({"graph": None, "facts": None}))

    with pytest.raises(StorageError, match="not an Ontos NetworkxStore v1 file"):
        NetworkxStore(path=pkl)


def test_load_fails_loud_on_corrupt_payload(tmp_path: Path) -> None:
    pkl = tmp_path / "corrupt.pkl"
    pkl.write_bytes(NetworkxStore._MAGIC_HEADER + b"this is not a pickle")

    with pytest.raises(StorageError, match="corrupt payload"):
        NetworkxStore(path=pkl)


def test_load_fails_loud_on_unrecognised_shape(tmp_path: Path) -> None:
    pkl = tmp_path / "wrong-shape.pkl"
    pkl.write_bytes(NetworkxStore._MAGIC_HEADER + pickle.dumps({"wrong_key": 42}))

    with pytest.raises(StorageError, match="payload shape"):
        NetworkxStore(path=pkl)


async def test_atomic_swap_leaves_previous_file_intact_on_mid_save_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside flush must not clobber a pre-existing valid file."""
    pkl = tmp_path / "state.pkl"

    # Prime a known-good file.
    writer = NetworkxStore(path=pkl)
    await writer.add_fact(_fact("alice", "works_at", "acme"))
    writer.flush()
    good_bytes = pkl.read_bytes()
    assert good_bytes.startswith(NetworkxStore._MAGIC_HEADER)

    # Mutate state and simulate a crash INSIDE write_bytes (so the
    # atomic replace never happens).
    await writer.add_fact(_fact("bob", "works_at", "beta"))

    real_write_bytes = Path.write_bytes

    def _boom(self: Path, data: bytes) -> int:
        if self.suffix == ".tmp":
            raise OSError("disk gremlin")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _boom)

    with pytest.raises(OSError, match="disk gremlin"):
        writer.flush()

    # Original file untouched.
    assert pkl.read_bytes() == good_bytes


async def test_close_persists_then_clears_in_memory_state(tmp_path: Path) -> None:
    pkl = tmp_path / "cleared.pkl"
    store = NetworkxStore(path=pkl)
    await store.add_fact(_fact("alice", "works_at", "acme"))
    await store.close()
    # In-memory state wiped (clear() happens after flush)
    assert len(store._facts) == 0
    # But the pickle is on disk.
    reader = NetworkxStore(path=pkl)
    hits = await reader.facts_for_entity("alice")
    assert len(hits) == 1


async def test_persistence_round_trips_bitemporal_supersession(tmp_path: Path) -> None:
    """Supersession state (t_invalid, superseded_by) must survive a round trip."""
    pkl = tmp_path / "bitemp.pkl"

    writer = NetworkxStore(path=pkl)
    original = _fact("alice", "works_at", "acme")
    await writer.add_fact(original)
    await writer.close_fact(original.id, t_invalid=datetime(2026, 6, 1), superseded_by=uuid4())
    writer.flush()

    reader = NetworkxStore(path=pkl)
    recovered = await reader.get_fact(original.id)
    assert recovered is not None
    assert recovered.t_invalid == datetime(2026, 6, 1)
    assert recovered.superseded_by is not None


async def test_persistence_creates_parent_directory(tmp_path: Path) -> None:
    """flush() creates ~/.ontos/ (or any parent dir) on first write."""
    deep = tmp_path / "nested" / "a" / "b" / "c" / "store.pkl"
    assert not deep.parent.exists()
    store = NetworkxStore(path=deep)
    await store.add_fact(_fact("alice", "works_at", "acme"))
    store.flush()
    assert deep.exists()
    assert deep.parent.is_dir()
