"""Prevent Bloom exports from targeting the operational Neo4j database."""

from dataclasses import replace

import pytest
import typer

from ontos.cli.main import _reject_operational_push_target
from ontos.runtime.config import RuntimeConfig


@pytest.fixture
def neo4j_config():
    return replace(
        RuntimeConfig.from_env(),
        storage_backend="neo4j",
        storage_path=None,
        neo4j_uri="bolt://localhost:7687",
        neo4j_database="operational",
    )


def test_rejects_operational_database(neo4j_config):
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(
            neo4j_config,
            "bolt://localhost:7687",
            "operational",
        )


def test_allows_separate_visualization_database(neo4j_config):
    _reject_operational_push_target(
        neo4j_config,
        "bolt://localhost:7687",
        "visualization",
    )


def test_allows_separate_neo4j_server(neo4j_config):
    _reject_operational_push_target(
        neo4j_config,
        "bolt://visualization-host:7687",
        "operational",
    )
