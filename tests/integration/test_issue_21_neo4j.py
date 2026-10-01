"""End-to-end Neo4j repro of GitHub issue #21 (bitemporal supersession).

The unit tests prove the fix against NetworkxStore. This file proves
it against real Neo4j — the backend the reporter hit the bug on.

Three tests:
  1. Reporter's exact scenario — ingest "David Lee works at Beta
     Systems", then ingest "David Lee works at Acme Corp"; the Beta
     fact must end up with t_invalid set and superseded_by pointing
     at the Acme fact. Current store.get_fact lookups confirm the
     state matches what the reporter asked for.
  2. Bitemporal correctness — as_of before the correction returns
     Beta; as_of after returns Acme.
  3. many_to_many relations stack and never trigger supersession.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontos.extraction import LlmExtractor, RawTriple
from ontos.ingest import TextConnector
from ontos.ontology import Ontology, load_ontology
from ontos.pipeline import IngestPipeline
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)


class ScriptedLLM:
    """Minimal LLM backend that returns a scripted triple per text."""

    id: str = "neo4j-issue-21-scripted"
    version: str = "test-0"

    def __init__(self, script: dict[str, list[RawTriple]]) -> None:
        self._script = script

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        return list(self._script.get(text, []))


def _triple(
    subject: str,
    s_type: str,
    s_name: str,
    predicate: str,
    obj: str,
    o_type: str,
    o_name: str,
) -> RawTriple:
    return RawTriple(
        subject_id=subject,
        subject_type=s_type,
        subject_canonical_name=s_name,
        predicate=predicate,
        object_id=obj,
        object_type=o_type,
        object_canonical_name=o_name,
        llm_confidence=0.95,
    )


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    return load_ontology(STARTER_PATH)


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
async def store(neo4j_container) -> AsyncIterator[Neo4jStore]:
    """Fresh, empty Neo4j store for each test."""
    uri = neo4j_container.get_connection_url()
    password = neo4j_container.password
    s = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await s.initialize()
    async with s._driver.session(database=s._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield s
    finally:
        await s.close()


# ────────────────────────────────────────────────────────────────────


async def test_issue_21_second_ingest_closes_prior_fact_against_neo4j(
    store: Neo4jStore, ontology: Ontology
) -> None:
    """Reporter's exact repro end-to-end on real Neo4j."""
    script = {
        "David Lee works at Beta Systems.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:beta",
                "Company",
                "Beta Systems",
            )
        ],
        "David Lee works at Acme Corp.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:acme",
                "Company",
                "Acme Corp",
            )
        ],
    }
    extractor = LlmExtractor(ScriptedLLM(script), ontology)

    first = await IngestPipeline(
        connector=TextConnector({"test.txt": "David Lee works at Beta Systems."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    assert first.facts_written == 1
    assert first.facts_superseded == 0

    beta_before = [
        f for f in await store.facts_for_entity("person:david-lee") if f.t_invalid is None
    ]
    assert len(beta_before) == 1
    beta_fact = beta_before[0]

    second = await IngestPipeline(
        connector=TextConnector({"update.txt": "David Lee works at Acme Corp."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    assert second.facts_written == 1
    assert second.facts_superseded == 1

    # Beta fact must be closed with superseded_by set.
    beta_after = await store.get_fact(beta_fact.id)
    assert beta_after is not None
    assert beta_after.t_invalid is not None, (
        "issue #21: the old Beta fact must be closed on Neo4j, not left active"
    )
    assert beta_after.superseded_by is not None

    # Only the Acme fact remains active for David Lee's works_at.
    active = [f for f in await store.facts_for_entity("person:david-lee") if f.t_invalid is None]
    works_at_active = [f for f in active if f.predicate == "works_at"]
    assert len(works_at_active) == 1
    acme_fact = works_at_active[0]
    assert acme_fact.object_id == "company:acme"
    assert beta_after.superseded_by == acme_fact.id


async def test_bitemporal_as_of_against_neo4j(store: Neo4jStore, ontology: Ontology) -> None:
    """as_of(before correction) returns Beta; as_of(after) returns Acme."""
    script = {
        "David Lee works at Beta Systems.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:beta",
                "Company",
                "Beta Systems",
            )
        ],
        "David Lee works at Acme Corp.": [
            _triple(
                "person:david-lee",
                "Person",
                "David Lee",
                "works_at",
                "company:acme",
                "Company",
                "Acme Corp",
            )
        ],
    }
    extractor = LlmExtractor(ScriptedLLM(script), ontology)

    await IngestPipeline(
        connector=TextConnector({"test.txt": "David Lee works at Beta Systems."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    before_correction = datetime.now(UTC)
    await asyncio.sleep(0.05)
    await IngestPipeline(
        connector=TextConnector({"update.txt": "David Lee works at Acme Corp."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    after_correction = datetime.now(UTC)

    historical = await store.facts_for_entity("person:david-lee", as_of=before_correction)
    historical_works_at = [f for f in historical if f.predicate == "works_at"]
    assert len(historical_works_at) == 1
    assert historical_works_at[0].object_id == "company:beta"

    current = await store.facts_for_entity("person:david-lee", as_of=after_correction)
    current_works_at = [f for f in current if f.predicate == "works_at"]
    assert len(current_works_at) == 1
    assert current_works_at[0].object_id == "company:acme"


async def test_many_to_many_relation_does_not_fire_supersession_against_neo4j(
    store: Neo4jStore, ontology: Ontology
) -> None:
    """`acquired` stacks on Neo4j; no supersession fires."""
    now = datetime.now(UTC)
    for obj in ("company:beta", "company:gamma"):
        await store.add_fact(
            Fact(
                subject_id="company:acme",
                predicate="acquired",
                object_id=obj,
                provenance=Provenance(
                    source_id="seed",
                    extractor_id="seed",
                    extractor_version="0.0.0",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
                t_valid=now,
                ingested_at=now,
            )
        )

    script = {
        "Acme acquired Delta.": [
            _triple(
                "company:acme",
                "Company",
                "Acme",
                "acquired",
                "company:delta",
                "Company",
                "Delta",
            )
        ],
    }
    extractor = LlmExtractor(ScriptedLLM(script), ontology)
    report = await IngestPipeline(
        connector=TextConnector({"d1": "Acme acquired Delta."}),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=ontology,
    ).run()
    assert report.facts_superseded == 0

    active = [f for f in await store.facts_for_entity("company:acme") if f.t_invalid is None]
    acquired_active = [f for f in active if f.predicate == "acquired"]
    assert len(acquired_active) == 3
