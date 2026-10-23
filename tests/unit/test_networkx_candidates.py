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
    results = await reader.list_candidates(allowed_acls=["team-private"])

    assert len(results) == 1
    assert results[0].raw_triple.predicate == "secret_relationship"
    assert results[0].provenance.source_id == "private-document"


async def test_candidate_writes_are_idempotent() -> None:
    store = NetworkxStore()
    candidate = _candidate()

    # Duplicates within one batch must not create multiple records.
    await store.add_candidates([candidate, candidate])

    # Repeated ingestion must not create another record.
    await store.add_candidates([candidate])

    results = await store.list_candidates(
        allowed_acls=["team-private"],
    )
    assert len(results) == 1
    assert results[0] == candidate


async def test_candidates_from_different_sources_remain_distinct() -> None:
    store = NetworkxStore()
    first = _candidate()
    second = first.model_copy(
        update={"provenance": first.provenance.model_copy(update={"source_id": "another-document"})}
    )

    await store.add_candidates([first, second, first, second])

    results = await store.list_candidates(
        allowed_acls=["team-private"],
    )

    assert len(results) == 2
    assert {item.provenance.source_id for item in results} == {
        "private-document",
        "another-document",
    }


async def test_candidates_with_different_acls_remain_distinct() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("team-private"),
            _candidate("team-other"),
            _candidate("team-private"),
        ]
    )

    private = await store.list_candidates(
        allowed_acls=["team-private"],
    )
    other = await store.list_candidates(
        allowed_acls=["team-other"],
    )

    assert len(private) == 1
    assert len(other) == 1


async def test_candidate_acl_filtering() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("team-private"),
            _candidate("team-other"),
            _candidate(None),
        ]
    )

    denied = await store.list_candidates(allowed_acls=[])
    assert denied == []

    permitted = await store.list_candidates(allowed_acls=["team-private"])
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

    assert (
        await store.search(
            "secret_relationship",
            allowed_acls=["team-private"],
        )
        == []
    )

    assert (
        list(
            await store.traverse(
                "secret-person",
                allowed_acls=["team-private"],
            )
        )
        == []
    )

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

    assert await store.list_candidates(allowed_acls=["team-private"]) == []

    await store.add_candidates([_candidate()])
    store.flush()

    assert path.read_bytes().startswith(b"ONTOS-NX-STORE-V3\n")

    reloaded = NetworkxStore(path=path)
    assert len(await reloaded.list_candidates(allowed_acls=["team-private"])) == 1


async def test_close_clears_candidates_after_persisting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.pkl"
    store = NetworkxStore(path=path)

    await store.add_candidates([_candidate()])
    await store.close()

    assert await store.list_candidates(allowed_acls=["team-private"]) == []

    reader = NetworkxStore(path=path)
    assert len(await reader.list_candidates(allowed_acls=["team-private"])) == 1


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


async def test_candidate_cursor_pagination() -> None:
    from ontos.storage.base import encode_candidate_cursor

    store = NetworkxStore()
    original = _candidate()

    candidates = [
        original.model_copy(
            update={
                "provenance": original.provenance.model_copy(
                    update={"source_id": f"source-{i:03d}"}
                )
            }
        )
        for i in range(7)
    ]

    # Insert deliberately out of order.
    await store.add_candidates(list(reversed(candidates)))

    retrieved = []
    after = None

    while True:
        page = await store.list_candidates(
            allowed_acls=["team-private"],
            limit=2,
            after=after,
        )
        if not page:
            break

        retrieved.extend(page)
        after = encode_candidate_cursor(page[-1])

    assert [c.provenance.source_id for c in retrieved] == [f"source-{i:03d}" for i in range(7)]
    assert len(retrieved) == len(candidates)


async def test_candidate_pagination_preserves_acl_filtering() -> None:
    from ontos.storage.base import encode_candidate_cursor

    store = NetworkxStore()
    private = _candidate("team-private")
    forbidden = _candidate("team-other")

    await store.add_candidates([private, forbidden])

    first_page = await store.list_candidates(
        allowed_acls=["team-private"],
        limit=1,
    )
    assert first_page == [private]

    next_page = await store.list_candidates(
        allowed_acls=["team-private"],
        limit=1,
        after=encode_candidate_cursor(private),
    )
    assert next_page == []


async def test_invalid_candidate_cursor_is_rejected() -> None:
    store = NetworkxStore()

    with pytest.raises(ValueError, match="cursor"):
        await store.list_candidates(
            allowed_acls=["team-private"],
            after="not-a-valid-cursor",
        )


async def test_logical_candidate_identity_ignores_confidence() -> None:
    from ontos.storage.base import candidate_storage_id

    original = _candidate("team-private")

    updated_confidence = original.model_copy(
        update={
            "raw_triple": original.raw_triple.model_copy(update={"llm_confidence": 0.51}),
            "provenance": original.provenance.model_copy(update={"confidence_score": 0.51}),
        }
    )

    different_acl = original.model_copy(update={"acl_ref": "team-other"})

    assert candidate_storage_id(original) == candidate_storage_id(updated_confidence)
    assert candidate_storage_id(original) != candidate_storage_id(different_acl)

    store = NetworkxStore()

    await store.add_candidates([original])
    await store.add_candidates([updated_confidence])
    await store.add_candidates([different_acl])

    private = await store.list_candidates(
        allowed_acls=["team-private"],
    )
    other = await store.list_candidates(
        allowed_acls=["team-other"],
    )

    assert private == [original]
    assert other == [different_acl]


def _variant(i: int, source_id: str) -> RejectedCandidate:
    base = _candidate()
    return base.model_copy(
        update={
            "raw_triple": base.raw_triple.model_copy(update={"subject_id": f"s-{i}"}),
            "provenance": base.provenance.model_copy(update={"source_id": source_id}),
        }
    )


async def test_add_candidates_returns_number_newly_stored() -> None:
    store = NetworkxStore()
    batch = [_variant(1, "a"), _variant(2, "a"), _variant(1, "a")]

    assert await store.add_candidates(batch) == 2
    assert await store.add_candidates(batch) == 0
    assert await store.add_candidates([]) == 0


async def test_paging_large_store_returns_each_candidate_once_in_order() -> None:
    from ontos.storage.base import candidate_sort_key, encode_candidate_cursor

    store = NetworkxStore()
    candidates = [_variant(i, f"src-{i % 37:02d}") for i in range(1500)]
    for start in range(0, len(candidates), 50):
        await store.add_candidates(candidates[start : start + 50])

    seen = []
    after = None
    while True:
        page = await store.list_candidate_records(
            allowed_acls=["team-private"], limit=97, after=after
        )
        seen += page
        if len(page) < 97:
            break
        after = encode_candidate_cursor(page[-1].candidate)

    assert len(seen) == len(candidates)
    assert len({item.stored_id for item in seen}) == len(candidates)
    assert [candidate_sort_key(item.candidate) for item in seen] == sorted(
        candidate_sort_key(c) for c in candidates
    )


async def test_list_rejects_boolean_limit() -> None:
    store = NetworkxStore()
    with pytest.raises(ValueError):
        await store.list_candidate_records(allowed_acls=[], limit=True)


def test_candidate_source_id_must_be_non_empty() -> None:
    with pytest.raises(ValueError):
        _candidate().provenance.model_validate(
            {**_candidate().provenance.model_dump(), "source_id": ""}
        )
