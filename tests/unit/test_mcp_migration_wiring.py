"""M3-1: MCP server must use the verified migration executor."""

from dataclasses import replace
from pathlib import Path

import pytest

import ontos.runtime.server as server_module
from ontos.executor import DeterministicExecutor
from ontos.migration.loading import MigrationConfigurationError
from ontos.runtime.config import RuntimeConfig
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def _config(monkeypatch: pytest.MonkeyPatch) -> RuntimeConfig:
    monkeypatch.delenv("ONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_HISTORY_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_PATH", raising=False)
    monkeypatch.setenv("ONTOS_PLANNER_BACKEND", "none")
    monkeypatch.setenv("ONTOS_AUTHZ_BACKEND", "none")

    return RuntimeConfig.from_env()


def test_server_uses_shared_executor_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(monkeypatch)
    expected = DeterministicExecutor()
    received: list[RuntimeConfig] = []

    def factory(cfg: RuntimeConfig) -> DeterministicExecutor:
        received.append(cfg)
        return expected

    monkeypatch.setattr(
        server_module,
        "build_verified_executor",
        factory,
    )

    # Explicit dependencies avoid opening real persistent backends.
    server = server_module.build_server(
        config=config,
        store=NetworkxStore(),
        audit=object(),
    )

    assert server is not None
    assert received == [config]


def test_invalid_migration_config_blocks_server_before_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _config(monkeypatch),
        ontology_path=STARTER,
        migration_dir=tmp_path / "missing-migrations",
        ontology_history_dir=tmp_path / "missing-history",
    )

    opened: list[bool] = []

    def unexpected_store(cfg: RuntimeConfig) -> NetworkxStore:
        opened.append(True)
        raise AssertionError("storage opened before migration validation")

    monkeypatch.setattr(
        server_module,
        "build_store",
        unexpected_store,
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="migration directory does not exist",
    ):
        server_module.build_server(config=config)

    assert opened == []


def test_custom_executor_cannot_bypass_configured_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _config(monkeypatch),
        ontology_path=STARTER,
        migration_dir=tmp_path / "migrations",
        ontology_history_dir=tmp_path / "history",
    )

    with pytest.raises(
        MigrationConfigurationError,
        match="custom executor cannot bypass",
    ):
        server_module.build_server(
            config=config,
            executor=DeterministicExecutor(),
        )


def test_custom_executor_remains_supported_without_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(monkeypatch)

    def unexpected_factory(cfg: RuntimeConfig) -> DeterministicExecutor:
        raise AssertionError("explicit executor should be preserved")

    monkeypatch.setattr(
        server_module,
        "build_verified_executor",
        unexpected_factory,
    )

    server = server_module.build_server(
        config=config,
        executor=DeterministicExecutor(),
        store=NetworkxStore(),
        audit=object(),
    )

    assert server is not None
