"""#37 — CLI --storage Neo4j override against a real Neo4j 5 container."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from typer.testing import CliRunner

import ontos.cli.main as cli_main
import ontos.planner
from ontos.cli import app
from ontos.planner import Plan, SeedByEntity
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.neo4j_store import Neo4jStore

pytestmark = pytest.mark.integration

STARTER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
)

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


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


async def _seed_graph(uri: str, password: str) -> None:
    store = Neo4jStore.from_uri(uri, auth=("neo4j", password))
    await store.initialize()

    async with store._driver.session(database=store._database) as session:
        await session.run("MATCH (n) DETACH DELETE n")

    now = datetime.now(UTC)
    await store.add_fact(
        Fact(
            subject_id="person:alice",
            predicate="works_at",
            object_id="company:acme",
            provenance=Provenance(
                source_id="issue-37",
                extractor_id="integration-test",
                extractor_version="1",
                confidence=Confidence.EXTRACTED,
                confidence_score=1.0,
            ),
            t_valid=now,
            ingested_at=now,
        )
    )
    await store.close()


class _StubPlanner:
    def __init__(self, llm: object, ontology: object) -> None:
        self._ontology = ontology

    async def plan(self, question: str, ontology: object) -> Plan:
        return Plan.build(
            seed=SeedByEntity(entity_id="person:alice"),
            steps=[],
            ontology=ontology,  # type: ignore[arg-type]
        )


def test_storage_url_matches_environment_invocation_byte_for_byte(
    neo4j_container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same query must behave identically via env config and --storage."""
    connection_url = neo4j_container.get_connection_url()
    password = neo4j_container.password

    asyncio.run(_seed_graph(connection_url, password))

    parsed = urlsplit(connection_url)
    assert parsed.hostname is not None
    assert parsed.port is not None

    storage_url = f"neo4j://{parsed.hostname}:{parsed.port}/neo4j"

    monkeypatch.setattr(
        cli_main,
        "_build_llm",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(cli_main, "_echo_llm_usage", lambda llm: None)
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _StubPlanner)

    command = [
        "query",
        "Where does Alice work?",
        "--ontology",
        str(STARTER_PATH),
    ]

    env_result = runner.invoke(
        app,
        command,
        env={
            "ONTOS_STORAGE_BACKEND": "neo4j",
            "ONTOS_STORAGE_PATH": "",
            "NEO4J_URI": connection_url,
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": password,
            "NEO4J_DATABASE": "neo4j",
        },
    )

    cli_result = runner.invoke(
        app,
        [
            "--storage",
            storage_url,
            "--storage-password-env",
            "ISSUE37_NEO4J_PASSWORD",
            *command,
        ],
        env={
            "ONTOS_STORAGE_BACKEND": "networkx",
            "ONTOS_STORAGE_PATH": "",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "deliberately-wrong",
            "ISSUE37_NEO4J_PASSWORD": password,
        },
    )

    assert env_result.exit_code == 0, env_result.stdout + env_result.stderr
    assert cli_result.exit_code == 0, cli_result.stdout + cli_result.stderr

    assert cli_result.stdout == env_result.stdout
    assert cli_result.stderr == env_result.stderr
