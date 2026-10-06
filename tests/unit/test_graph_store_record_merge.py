"""#46: GraphStore.record_merge + merges_for_entity — unit coverage.

Verifies the Protocol methods, the dict-fan-out semantics (one logical
record stored under multiple (canonical, merged, resolver) keys), bidirectional
lookup, idempotence, last-write-wins on resolved_at, self-loop skipping
(ExactMatchResolver convention), the _dirty flag, V1→V2 pickle migration,
and pipeline wiring.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path

import structlog

from ontos.resolver.base import MergeRecord
from ontos.storage.networkx_store import NetworkxStore


def _merge(
    canonical: str = "alice",
    merged: list[str] | None = None,
    resolver_id: str = "ontos.resolver.exact-match",
    resolver_version: str = "0.1.0",
    resolved_at: datetime | None = None,
    reason: str = "exact match on (type, casefolded canonical_name)",
) -> MergeRecord:
    return MergeRecord(
        canonical_id=canonical,
        merged_ids=merged if merged is not None else [canonical, "alice-alt"],
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        resolved_at=resolved_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        reason=reason,
    )


def test_networkx_store_has_record_merge_and_merges_for_entity() -> None:
    """Protocol change is additive — NetworkxStore implements both methods."""
    store = NetworkxStore()
    assert hasattr(store, "record_merge")
    assert hasattr(store, "merges_for_entity")
    assert callable(store.record_merge)
    assert callable(store.merges_for_entity)


async def test_record_merge_stores_record_and_merges_for_entity_reads_it_back() -> None:
    """Round-trip from both endpoints — canonical AND merged ids find the record."""
    store = NetworkxStore()
    rec = _merge(canonical="alice", merged=["alice", "alice-alt"])
    await store.record_merge(rec)

    from_canonical = await store.merges_for_entity("alice")
    assert len(from_canonical) == 1
    assert from_canonical[0] == rec

    from_merged = await store.merges_for_entity("alice-alt")
    assert len(from_merged) == 1
    assert from_merged[0] == rec


async def test_record_merge_skips_self_loop_but_persists_real_merges() -> None:
    """ExactMatchResolver includes canonical_id in merged_ids; we skip the
    self-loop so the audit view only shows real consolidations."""
    store = NetworkxStore()
    rec = _merge(canonical="alice", merged=["alice", "alice-alt"])
    await store.record_merge(rec)

    # One key for ("alice", "alice-alt", resolver_id); the ("alice", "alice", ...)
    # key is deliberately absent.
    keys = list(store._merges.keys())  # noqa: SLF001
    assert len(keys) == 1
    canonical, merged, _resolver = keys[0]
    assert canonical == "alice"
    assert merged == "alice-alt"


async def test_record_merge_fans_out_across_multiple_merged_ids() -> None:
    """One logical record with 3 merged ids → 2 keys (one per non-self id),
    but merges_for_entity returns the record once from each endpoint."""
    store = NetworkxStore()
    rec = _merge(canonical="A", merged=["A", "B", "C"])
    await store.record_merge(rec)

    assert len(store._merges) == 2  # noqa: SLF001 — ("A","B",...) and ("A","C",...)
    for probe_id in ("A", "B", "C"):
        hits = await store.merges_for_entity(probe_id)
        assert len(hits) == 1
        assert hits[0] == rec


async def test_record_merge_is_idempotent_on_canonical_merged_resolver_tuple() -> None:
    """Re-recording the same (canonical, merged, resolver) is last-write-wins
    on resolved_at — one record, not two."""
    store = NetworkxStore()
    first = _merge(
        canonical="A",
        merged=["A", "B"],
        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason="first assertion",
    )
    second = _merge(
        canonical="A",
        merged=["A", "B"],
        resolved_at=datetime(2026, 6, 1, tzinfo=UTC),
        reason="re-affirmed",
    )
    await store.record_merge(first)
    await store.record_merge(second)

    assert len(store._merges) == 1  # noqa: SLF001
    hits = await store.merges_for_entity("A")
    assert len(hits) == 1
    # Last write wins.
    assert hits[0].resolved_at == datetime(2026, 6, 1, tzinfo=UTC)
    assert hits[0].reason == "re-affirmed"


async def test_different_resolver_ids_coexist_as_separate_records() -> None:
    """Two resolver tiers both asserting the same merge produce two records,
    not one — a future ML tier can see what an earlier Rules tier decided."""
    store = NetworkxStore()
    rules = _merge(canonical="A", merged=["A", "B"], resolver_id="ontos.resolver.exact-match")
    ml = _merge(canonical="A", merged=["A", "B"], resolver_id="ontos.resolver.ml")
    await store.record_merge(rules)
    await store.record_merge(ml)

    assert len(store._merges) == 2  # noqa: SLF001
    hits = await store.merges_for_entity("A")
    resolver_ids = sorted(r.resolver_id for r in hits)
    assert resolver_ids == ["ontos.resolver.exact-match", "ontos.resolver.ml"]


async def test_merges_for_entity_returns_empty_list_for_unknown_id() -> None:
    store = NetworkxStore()
    await store.record_merge(_merge())
    hits = await store.merges_for_entity("not-mentioned")
    assert hits == []


async def test_record_merge_sets_dirty_flag() -> None:
    store = NetworkxStore()
    assert store._dirty is False  # noqa: SLF001
    await store.record_merge(_merge())
    assert store._dirty is True  # noqa: SLF001


async def test_record_merge_round_trips_through_pickle_persistence(tmp_path: Path) -> None:
    pkl = tmp_path / "store.pkl"
    writer = NetworkxStore(path=pkl)
    await writer.record_merge(_merge(canonical="A", merged=["A", "B"]))
    await writer.close()

    reader = NetworkxStore(path=pkl)
    hits = await reader.merges_for_entity("A")
    assert len(hits) == 1
    assert hits[0].canonical_id == "A"
    assert hits[0].merged_ids == ["A", "B"]


async def test_v1_pickle_loads_cleanly_with_empty_merges(
    tmp_path: Path,
) -> None:
    """V1→V2 migration: a legacy pickle (no 'merges' key) loads cleanly;
    _merges defaults to empty; a structlog info line names the migration."""
    pkl = tmp_path / "legacy.pkl"
    import networkx as nx

    legacy_payload = pickle.dumps({"graph": nx.MultiDiGraph(), "facts": {}})
    pkl.write_bytes(b"ONTOS-NX-STORE-V1\n" + legacy_payload)

    with structlog.testing.capture_logs() as logs:
        store = NetworkxStore(path=pkl)

    assert store._merges == {}  # noqa: SLF001
    migration_events = [
        log for log in logs if log.get("event") == "networkx-store-migrated-from-legacy-format"
    ]
    assert len(migration_events) == 1
    assert migration_events[0]["from_version"] == "ONTOS-NX-STORE-V1"
    assert migration_events[0]["to_version"] == "ONTOS-NX-STORE-V2"


async def test_v1_pickle_upgrades_to_v2_on_flush(tmp_path: Path) -> None:
    """After loading a V1 file and recording a new merge, the next flush
    writes the current (V2) magic header + the full payload with merges."""
    pkl = tmp_path / "upgrade.pkl"
    import networkx as nx

    legacy_payload = pickle.dumps({"graph": nx.MultiDiGraph(), "facts": {}})
    pkl.write_bytes(b"ONTOS-NX-STORE-V1\n" + legacy_payload)

    store = NetworkxStore(path=pkl)
    await store.record_merge(_merge(canonical="A", merged=["A", "B"]))
    await store.close()

    raw = pkl.read_bytes()
    assert raw.startswith(b"ONTOS-NX-STORE-V2\n")

    # Re-read confirms the merge survived under the new format.
    reader = NetworkxStore(path=pkl)
    hits = await reader.merges_for_entity("A")
    assert len(hits) == 1
