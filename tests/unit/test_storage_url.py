"""#37 — storage URL parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.runtime.config import RuntimeConfig, StorageConfig


def test_networkx_memory_url() -> None:
    cfg = StorageConfig.from_url("networkx://")

    assert cfg.backend == "networkx"
    assert cfg.path is None
    assert cfg.neo4j_uri is None


def test_networkx_persisted_url() -> None:
    cfg = StorageConfig.from_url("networkx:///tmp/scratch.pkl")

    assert cfg.backend == "networkx"
    assert cfg.path == Path("/tmp/scratch.pkl")
    assert cfg.neo4j_uri is None


def test_neo4j_url_parses_connection_and_database() -> None:
    cfg = StorageConfig.from_url("neo4j://alice:secret@db.example:7687/knowledge")

    assert cfg.backend == "neo4j"
    assert cfg.path is None
    assert cfg.neo4j_uri == "neo4j://db.example:7687"
    assert cfg.neo4j_user == "alice"
    assert cfg.neo4j_password == "secret"
    assert cfg.neo4j_database == "knowledge"


@pytest.mark.parametrize(
    ("url", "expected_uri"),
    [
        (
            "neo4j+ssc://db.example:7687/knowledge",
            "neo4j+ssc://db.example:7687",
        ),
        (
            "bolt+s://db.example:7687/knowledge",
            "bolt+s://db.example:7687",
        ),
    ],
)
def test_neo4j_tls_schemes(url: str, expected_uri: str) -> None:
    cfg = StorageConfig.from_url(url)

    assert cfg.backend == "neo4j"
    assert cfg.neo4j_uri == expected_uri
    assert cfg.neo4j_database == "knowledge"


def test_neo4j_url_allows_credentials_to_be_filled_later() -> None:
    cfg = StorageConfig.from_url("neo4j://db.example:7687/knowledge")

    assert cfg.neo4j_user is None
    assert cfg.neo4j_password is None


def test_neo4j_url_requires_database() -> None:
    with pytest.raises(ValueError, match="database"):
        StorageConfig.from_url("neo4j://db.example:7687")


def test_storage_url_rejects_bad_port() -> None:
    with pytest.raises(ValueError, match="port"):
        StorageConfig.from_url("neo4j://db.example:notaport/knowledge")


def test_storage_url_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        StorageConfig.from_url("postgresql://db.example/knowledge")


def test_storage_config_repr_never_exposes_password() -> None:
    cfg = StorageConfig.from_url("neo4j://alice:super-secret@db.example:7687/knowledge")

    assert "super-secret" not in repr(cfg)


def test_storage_override_networkx_memory_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "neo4j")
    monkeypatch.setenv("ONTOS_STORAGE_PATH", "/env/store.pkl")
    monkeypatch.setenv("NEO4J_URI", "neo4j://env-host:7687")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")
    monkeypatch.setenv("NEO4J_DATABASE", "env-db")

    base = RuntimeConfig.from_env()
    override = StorageConfig.from_url("networkx://")
    cfg = base.with_storage_override(override)

    assert cfg.storage_backend == "networkx"
    assert cfg.storage_path is None


def test_storage_override_networkx_path_wins_over_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.setenv("ONTOS_STORAGE_PATH", "/env/store.pkl")

    base = RuntimeConfig.from_env()
    target = tmp_path / "cli.pkl"
    override = StorageConfig.from_url(f"networkx://{target}")
    cfg = base.with_storage_override(override)

    assert cfg.storage_backend == "networkx"
    assert cfg.storage_path == target


def test_storage_override_neo4j_cli_values_win_env_fills_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.setenv("NEO4J_USER", "env-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")

    base = RuntimeConfig.from_env()
    override = StorageConfig.from_url("neo4j://cli-host:7687/cli-db")
    cfg = base.with_storage_override(override)

    assert cfg.storage_backend == "neo4j"
    assert cfg.storage_path is None
    assert cfg.neo4j_uri == "neo4j://cli-host:7687"
    assert cfg.neo4j_database == "cli-db"
    assert cfg.neo4j_user == "env-user"
    assert cfg.neo4j_password == "env-secret"


def test_storage_override_neo4j_url_credentials_win_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_USER", "env-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")

    base = RuntimeConfig.from_env()
    override = StorageConfig.from_url("neo4j://cli-user:cli-secret@cli-host:7687/cli-db")
    cfg = base.with_storage_override(override)

    assert cfg.neo4j_user == "cli-user"
    assert cfg.neo4j_password == "cli-secret"
