"""#37 — build_store must consume RuntimeConfig, not reread storage env vars."""

from __future__ import annotations

import dataclasses

import pytest

from ontos.runtime.config import RuntimeConfig
from ontos.runtime.server import build_store


def test_build_store_neo4j_uses_runtime_config_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = RuntimeConfig.from_env()
    cfg = dataclasses.replace(
        base,
        storage_backend="neo4j",
        storage_path=None,
        neo4j_uri="neo4j://config-host:7687",
        neo4j_user="config-user",
        neo4j_password="config-password",
        neo4j_database="config-db",
    )

    # Deliberately conflicting environment values: these must be ignored
    # once RuntimeConfig has been constructed.
    monkeypatch.setenv("NEO4J_URI", "neo4j://env-host:7687")
    monkeypatch.setenv("NEO4J_USER", "env-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-password")
    monkeypatch.setenv("NEO4J_DATABASE", "env-db")

    calls: list[tuple[str, tuple[str, str], str]] = []

    sentinel = object()

    def fake_from_uri(
        uri: str,
        *,
        auth: tuple[str, str],
        database: str = "neo4j",
    ) -> object:
        calls.append((uri, auth, database))
        return sentinel

    monkeypatch.setattr(
        "ontos.storage.neo4j_store.Neo4jStore.from_uri",
        fake_from_uri,
    )

    store = build_store(cfg)

    assert store is sentinel
    assert calls == [
        (
            "neo4j://config-host:7687",
            ("config-user", "config-password"),
            "config-db",
        )
    ]


def test_build_store_neo4j_requires_uri_and_password() -> None:
    base = RuntimeConfig.from_env()

    missing_uri = dataclasses.replace(
        base,
        storage_backend="neo4j",
        neo4j_uri=None,
        neo4j_password="secret",
    )
    with pytest.raises(RuntimeError, match="NEO4J_URI"):
        build_store(missing_uri)

    missing_password = dataclasses.replace(
        base,
        storage_backend="neo4j",
        neo4j_uri="neo4j://localhost:7687",
        neo4j_password=None,
    )
    with pytest.raises(RuntimeError, match="NEO4J_PASSWORD"):
        build_store(missing_password)


def test_server_main_uses_supplied_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    import ontos.runtime.server as runtime_server

    base = RuntimeConfig.from_env()
    cfg = dataclasses.replace(
        base,
        storage_backend="neo4j",
        neo4j_uri="neo4j://config-host:7687",
        neo4j_user="config-user",
        neo4j_password="config-password",
        neo4j_database="config-db",
        listen_host="127.0.0.9",
        listen_port=9999,
    )

    # Contradictory env values prove main() does not rebuild the config.
    monkeypatch.setenv("ONTOS_STORAGE_BACKEND", "networkx")
    monkeypatch.setenv("ONTOS_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("ONTOS_LISTEN_PORT", "1234")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-signing-key-not-for-prod")

    fake_server = object()
    seen: dict[str, object] = {}

    def fake_build_server(config: RuntimeConfig, *, audit: object) -> object:
        seen["config"] = config
        seen["build_audit"] = audit
        return fake_server

    def fake_run_server(
        server: object,
        *,
        host: str,
        port: int,
        mode: str,
        backend: str,
        auth_mode: str,
        allow_unauthenticated_bind: bool,
        audit: object,
    ) -> None:
        seen["server"] = server
        seen["host"] = host
        seen["port"] = port
        seen["mode"] = mode
        seen["backend"] = backend
        seen["auth_mode"] = auth_mode
        seen["allow_unauthenticated_bind"] = allow_unauthenticated_bind
        seen["run_audit"] = audit

    monkeypatch.setattr(runtime_server, "_default_dev_signing_key_if_missing", lambda: None)
    monkeypatch.setattr(runtime_server, "build_server", fake_build_server)
    monkeypatch.setattr(runtime_server, "run_server", fake_run_server)

    runtime_server.main(config=cfg)

    # The transport-rejection middleware must write to the same audit
    # chain as the tools, so both sides receive one emitter.
    assert seen.pop("build_audit") is seen.pop("run_audit")
    assert seen == {
        "config": cfg,
        "server": fake_server,
        "host": "127.0.0.9",
        "port": 9999,
        "mode": "live",
        "backend": "neo4j",
        "auth_mode": "none",
        "allow_unauthenticated_bind": False,
    }
