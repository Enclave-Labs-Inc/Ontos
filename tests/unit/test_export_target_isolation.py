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


# #74: aliases of the operational endpoint must be rejected too.


@pytest.mark.parametrize(
    "push_uri",
    [
        "bolt://127.0.0.1:7687",
        "neo4j://127.0.0.2:7687",
        "bolt://[::1]:7687",
        "bolt://LOCALHOST.:7687",
    ],
)
def test_rejects_loopback_aliases(neo4j_config, push_uri):
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(neo4j_config, push_uri, "operational")


def test_rejects_database_name_case_variant(neo4j_config):
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(neo4j_config, "bolt://localhost:7687", "Operational")


def test_rejects_hostname_resolving_to_operational_address(neo4j_config, monkeypatch):
    import socket

    config = replace(neo4j_config, neo4j_uri="bolt://10.0.0.5:7687")

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "graph.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(config, "bolt://graph.internal:7687", "operational")


def test_guard_applies_when_exporting_from_networkx(neo4j_config):
    networkx_source = replace(neo4j_config, storage_backend="networkx")
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(networkx_source, "bolt://localhost:7687", "operational")


def test_allows_different_port_on_same_host(neo4j_config):
    _reject_operational_push_target(neo4j_config, "bolt://localhost:7688", "operational")


def test_unresolvable_push_host_is_compared_literally(neo4j_config, monkeypatch):
    import socket

    def offline(*args, **kwargs):
        raise socket.gaierror("offline")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    _reject_operational_push_target(neo4j_config, "bolt://viz.internal:7687", "operational")
    with pytest.raises(typer.BadParameter, match="separate Neo4j"):
        _reject_operational_push_target(neo4j_config, "bolt://127.0.0.1:7687", "operational")
