"""End-to-end Neo4j repro of GitHub issue #16.

The unit-test regression suite proved the fix against NetworkxStore.
This test proves it against real Neo4j — the storage backend the
original bug was reported with.

Same scenario:
  - Store contains: 'Alice Johnson' --works_at--> 'Acme Corp'
  - LLM planner emits SeedByEntity(entity_id="Alice Johnson (Person)")
  - Executor must resolve "Alice Johnson (Person)" → "Alice Johnson"
    via type-suffix strip and return the works_at fact, with the
    resolution visible in warnings.

Also pins the two sister invariants against Neo4j:
  - Exact-match seed still resolves cleanly (no false-positive warning).
  - Ambiguity guard refuses to guess when multiple entities match.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.executor import DeterministicExecutor
from ontos.ontology import load_ontology
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


@pytest.fixture(scope="module")
def neo4j_container():
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


@pytest.fixture
async def alice_store(neo4j_container) -> AsyncIterator[Neo4jStore]:
    """Fresh Neo4j store holding only `Alice Johnson --works_at--> Acme Corp`."""
    uri = neo4j_container.get_connection_url()
    password = neo4j_container.password
    s = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await s.initialize()
    async with s._driver.session(database=s._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")

    now = datetime.now(UTC)
    await s.add_fact(
        Fact(
            subject_id="Alice Johnson",
            predicate="works_at",
            object_id="Acme Corp",
            provenance=Provenance(
                source_id="repro-doc",
                extractor_id="repro",
                extractor_version="0.0.0",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=now,
            ingested_at=now,
        )
    )
    try:
        yield s
    finally:
        await s.close()


async def test_issue_16_type_suffix_resolves_against_neo4j(
    alice_store: Neo4jStore,
) -> None:
    """The exact bug report, now against real Neo4j."""
    ontology = load_ontology(STARTER_PATH)

    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson (Person)"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits, (
        "Issue #16 regression: 'Alice Johnson (Person)' must resolve to "
        "'Alice Johnson' against Neo4j, not produce empty hits"
    )
    assert any(
        h.fact.subject_id == "Alice Johnson" and h.fact.object_id == "Acme Corp"
        for h in result.hits
    )
    assert any(
        "resolved to 'Alice Johnson'" in w and "type-suffix strip" in w for w in result.warnings
    ), f"resolution must surface in warnings; got warnings={result.warnings}"


async def test_exact_match_against_neo4j_no_false_positive_warning(
    alice_store: Neo4jStore,
) -> None:
    """An exact-id seed must not fire a resolution warning on Neo4j."""
    ontology = load_ontology(STARTER_PATH)

    plan = Plan.build(
        seed=SeedByEntity(entity_id="Alice Johnson"),
        steps=[TraversalStep(depth=1)],
        ontology=ontology,
    )
    result = await DeterministicExecutor().execute(plan, alice_store)

    assert result.hits
    assert not any("resolved to" in w for w in result.warnings)


async def test_ambiguity_guard_against_neo4j(
    neo4j_container,
) -> None:
    """Multiple substring-matching candidates against Neo4j → refuse to guess."""
    uri = neo4j_container.get_connection_url()
    password = neo4j_container.password
    s = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await s.initialize()
    async with s._driver.session(database=s._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")

    now = datetime.now(UTC)
    for subj, obj in (
        ("Alice", "Acme Corp"),
        ("Bob", "Acme Corp Europe"),
        ("Carol", "Acme Corp Holdings"),
    ):
        await s.add_fact(
            Fact(
                subject_id=subj,
                predicate="works_at",
                object_id=obj,
                provenance=Provenance(
                    source_id="repro-doc",
                    extractor_id="repro",
                    extractor_version="0.0.0",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
                t_valid=now,
                ingested_at=now,
            )
        )

    try:
        ontology = load_ontology(STARTER_PATH)
        plan = Plan.build(
            seed=SeedByEntity(entity_id="acme corp"),
            steps=[TraversalStep(depth=1)],
            ontology=ontology,
        )
        result = await DeterministicExecutor().execute(plan, s)

        assert result.hits == [], (
            "ambiguous keyword fallback must not silently pick one against Neo4j"
        )
        ambiguity = [w for w in result.warnings if "matches multiple graph entities" in w]
        assert ambiguity, f"ambiguity warning must fire; got warnings={result.warnings}"
        warning_body = ambiguity[0]
        for name in ("Acme Corp", "Acme Corp Europe", "Acme Corp Holdings"):
            assert name in warning_body
    finally:
        await s.close()
