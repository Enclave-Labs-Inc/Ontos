"""M1: rejected candidates remain separate from the trusted graph."""

from __future__ import annotations

import pickle
from pathlib import Path

import networkx as nx
import pytest

from ontos.extraction import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
)
from ontos.storage.networkx_store import NetworkxStore


def _candidate(
    acl_ref: str | None = "team-private",
) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="secret-person",
            subject_type="Person",
            subject_canonical_name="Secret Person",
            predicate="secret_relationship",
            object_id="secret-company",
            object_type="Company",
            object_canonical_name="Secret Company",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="private-document",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.9,
        ),
        acl_ref=acl_ref,
    )


async def test_candidates_round_trip_v3(tmp_path: Path) -> None:
    path = tmp_path / "store.pkl"
    writer = NetworkxStore(path=path)

    await writer.add_candidates([_candidate()])
    writer.flush()

    assert path.read_bytes().startswith(b"ONTOS-NX-STORE-V3\n")

    reader = NetworkxStore(path=path)
    results = await reader.list_candidates(
        allowed_acls=["team-private"]
    )

    assert len(results) == 1
    assert results[0].raw_triple.predicate == "secret_relationship"
    assert results[0].provenance.source_id == "private-document"


async def test_candidate_acl_filtering() -> None:
    store = NetworkxStore()
    await store.add_candidates([
        _candidate("team-private"),
        _candidate("team-other"),
        _candidate(None),
    ])

    denied = await store.list_candidates(allowed_acls=[])
    assert denied == []

    permitted = await store.list_candidates(
        allowed_acls=["team-private"]
    )
    assert len(permitted) == 1
    assert permitted[0].acl_ref == "team-private"

    public = await store.list_candidates(
        allowed_acls=[],
        include_public=True,
    )
    assert len(public) == 1
    assert public[0].acl_ref is None

    all_visible = await store.list_candidates(
        allowed_acls=["team-private"],
        include_public=True,
    )
    assert len(all_visible) == 2


async def test_rejected_candidates_never_enter_graph() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    assert list(store._graph.nodes) == []
    assert list(store._graph.edges) == []
    assert store._facts == {}

    assert await store.search(
        "secret_relationship",
        allowed_acls=["team-private"],
    ) == []

    assert list(await store.traverse(
        "secret-person",
        allowed_acls=["team-private"],
    )) == []

    scan = await store.scan(include_all_acls=True)
    assert scan.entities == []
    assert scan.facts == []
    assert scan.merges == []


@pytest.mark.parametrize("version", [1, 2])
async def test_legacy_stores_load_without_candidates(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / f"v{version}.pkl"

    payload = {
        "graph": nx.MultiDiGraph(),
        "facts": {},
    }
    if version == 2:
        payload["merges"] = {}

    header = f"ONTOS-NX-STORE-V{version}\n".encode()
    path.write_bytes(header + pickle.dumps(payload))

    store = NetworkxStore(path=path)

    assert await store.list_candidates(
        allowed_acls=["team-private"]
    ) == []

    await store.add_candidates([_candidate()])
    store.flush()

    assert path.read_bytes().startswith(b"ONTOS-NX-STORE-V3\n")

    reloaded = NetworkxStore(path=path)
    assert len(await reloaded.list_candidates(
        allowed_acls=["team-private"]
    )) == 1


async def test_close_clears_candidates_after_persisting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.pkl"
    store = NetworkxStore(path=path)

    await store.add_candidates([_candidate()])
    await store.close()

    assert store._candidates == []

    reader = NetworkxStore(path=path)
    assert len(await reader.list_candidates(
        allowed_acls=["team-private"]
    )) == 1


async def test_candidate_only_read_does_not_rewrite_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "readonly.pkl"
    writer = NetworkxStore(path=path)
    await writer.add_candidates([_candidate()])
    writer.flush()

    original_bytes = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns

    reader = NetworkxStore(path=path)
    await reader.list_candidates(allowed_acls=["team-private"])
    await reader.close()

    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime_ns == original_mtime


async def test_candidate_list_rejects_invalid_limit() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    with pytest.raises(ValueError, match="limit"):
        await store.list_candidates(
            allowed_acls=["team-private"],
            limit=0,
        )
