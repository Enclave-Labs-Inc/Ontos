"""RuntimeConfig env-var parsing — #29 added storage_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.runtime.config import RuntimeConfig


def test_storage_path_unset_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_STORAGE_PATH", raising=False)
    cfg = RuntimeConfig.from_env()
    assert cfg.storage_path is None


def test_storage_path_empty_string_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit opt-out via empty string must not auto-default."""
    monkeypatch.setenv("ONTOS_STORAGE_PATH", "")
    cfg = RuntimeConfig.from_env()
    assert cfg.storage_path is None


def test_storage_path_absolute_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.pkl"
    monkeypatch.setenv("ONTOS_STORAGE_PATH", str(target))
    cfg = RuntimeConfig.from_env()
    assert cfg.storage_path == target


def test_storage_path_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Path.expanduser() reads $HOME directly, so monkeypatch that."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONTOS_STORAGE_PATH", "~/some-store.pkl")
    cfg = RuntimeConfig.from_env()
    assert cfg.storage_path == tmp_path / "some-store.pkl"


def test_storage_backend_default_remains_networkx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protects against accidental default-flip during the #29 refactor."""
    monkeypatch.delenv("ONTOS_STORAGE_BACKEND", raising=False)
    cfg = RuntimeConfig.from_env()
    assert cfg.storage_backend == "networkx"
