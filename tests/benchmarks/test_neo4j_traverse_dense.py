"""Neo4j traverse stays bounded on dense graphs (#104). Non-gate.

The previous variable-length pattern enumerated every path, which is
exponential in depth: a 40-node clique at depth 4 holds ~2.2M simple
paths from one start. Breadth-first expansion touches each relationship
once per level.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from itertools import permutations

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]

_NODES = 40
_DEPTH = 4
_BUDGET_S = 10.0


@pytest.fixture(scope="module")
def neo4j_container():  # type: ignore[no-untyped-def]
    docker = pytest.importorskip("docker")
    testcontainers_neo4j = pytest.importorskip("testcontainers.neo4j")
    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker not reachable: {exc}")
    container = testcontainers_neo4j.Neo4jContainer("neo4j:5.24")
    container.start()
    try:
        yield container
    finally:
        container.stop()


async def test_dense_traverse_is_bounded(neo4j_container) -> None:  # type: ignore[no-untyped-def]
    from ontos.storage.neo4j_store import Neo4jStore

    store = Neo4jStore.from_uri(
        neo4j_container.get_connection_url(), auth=("neo4j", neo4j_container.password)
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    provenance = Provenance(
        source_id="bench",
        extractor_id="bench",
        extractor_version="0",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )
    try:
        await store.initialize()
        nodes = [f"n{i}" for i in range(_NODES)]
        for subject, obj in permutations(nodes, 2):
            await store.add_fact(
                Fact(
                    subject_id=subject,
                    predicate="linked",
                    object_id=obj,
                    provenance=provenance,
                    t_valid=now,
                    ingested_at=now,
                )
            )

        started = time.perf_counter()
        facts = list(await store.traverse("n0", direction="both", depth=_DEPTH))
        elapsed = time.perf_counter() - started

        print(f"\ndense traverse: {len(facts)} facts in {elapsed:.2f}s")
        assert len(facts) == _NODES * (_NODES - 1)
        assert elapsed < _BUDGET_S
    finally:
        await store.close()
