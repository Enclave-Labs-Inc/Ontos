"""M3-1 runtime configuration for verified ontology migrations."""

from pathlib import Path

import pytest

from ontos.runtime.config import RuntimeConfig


def test_migrations_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.delenv("ONTOS_ONTOLOGY_HISTORY_DIR", raising=False)

    config = RuntimeConfig.from_env()

    assert config.migration_dir is None
    assert config.ontology_history_dir is None


def test_migration_directories_load_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = tmp_path / "migrations"
    history = tmp_path / "history"

    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", str(migrations))
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", str(history))

    config = RuntimeConfig.from_env()

    assert config.migration_dir == migrations
    assert config.ontology_history_dir == history


@pytest.mark.parametrize(
    "missing",
    [
        "ONTOS_MIGRATIONS_DIR",
        "ONTOS_ONTOLOGY_HISTORY_DIR",
    ],
)
def test_partial_migration_configuration_is_refused(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", "/tmp/migrations")
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", "/tmp/history")
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match="must be configured together"):
        RuntimeConfig.from_env()


def test_empty_migration_environment_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", "")
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", "")

    config = RuntimeConfig.from_env()

    assert config.migration_dir is None
    assert config.ontology_history_dir is None


def test_migration_directories_expand_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", "~/ontos-migrations")
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", "~/ontos-history")

    config = RuntimeConfig.from_env()

    assert config.migration_dir == tmp_path / "ontos-migrations"
    assert config.ontology_history_dir == tmp_path / "ontos-history"
