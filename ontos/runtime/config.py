"""Runtime configuration.

Environment-driven. Nothing sensitive should live in config files at rest;
secrets come from the deploy environment (KMS-decrypted in production).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    storage_backend: str
    storage_path: Path | None
    audit_db_url: str
    audit_signing_key_env: str
    listen_host: str
    listen_port: int
    ontology_path: Path | None

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        # ONTOS_STORAGE_PATH: unset or empty → None (in-memory). An
        # explicit path (with ~ expansion) persists the dev store.
        # No auto-default here on purpose — tests that build via
        # from_env() → build_store() must stay in-memory. The CLI
        # layer applies the "~/.ontos/dev-store.pkl" default.
        raw_path = os.getenv("ONTOS_STORAGE_PATH", "")
        storage_path = Path(raw_path).expanduser() if raw_path else None

        return cls(
            storage_backend=os.getenv("ONTOS_STORAGE_BACKEND", "networkx"),
            storage_path=storage_path,
            audit_db_url=os.getenv(
                "ONTOS_AUDIT_DB_URL", "sqlite+pysqlite:///./audit-logs/audit.sqlite"
            ),
            audit_signing_key_env=os.getenv(
                "ONTOS_AUDIT_SIGNING_KEY_ENV", "ONTOS_AUDIT_SIGNING_KEY"
            ),
            listen_host=os.getenv("ONTOS_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(os.getenv("ONTOS_LISTEN_PORT", "8765")),
            ontology_path=(Path(p) if (p := os.getenv("ONTOS_ONTOLOGY_PATH")) else None),
        )
