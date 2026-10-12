"""Runtime configuration.

Environment-driven. Nothing sensitive should live in config files at rest;
secrets come from the deploy environment (KMS-decrypted in production).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True, repr=False)
class StorageConfig:
    """Parsed storage override from the CLI ``--storage`` URL."""

    backend: str
    path: Path | None = None
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: str | None = None
    neo4j_database: str | None = None

    def __repr__(self) -> str:
        """Diagnostic representation that never exposes credentials."""
        return (
            "StorageConfig("
            f"backend={self.backend!r}, "
            f"path={self.path!r}, "
            f"neo4j_uri={self.neo4j_uri!r}, "
            f"neo4j_user={self.neo4j_user!r}, "
            f"neo4j_password={'***' if self.neo4j_password is not None else None!r}, "
            f"neo4j_database={self.neo4j_database!r}"
            ")"
        )

    @classmethod
    def from_url(cls, url: str) -> StorageConfig:
        """Parse the storage URL grammar defined by issue #37."""
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()

        if scheme == "networkx":
            if parsed.netloc:
                raise ValueError("networkx storage URL must not include a host")
            if parsed.query or parsed.fragment:
                raise ValueError("networkx storage URL must not include query or fragment")

            if not parsed.path:
                return cls(backend="networkx")

            return cls(
                backend="networkx",
                path=Path(unquote(parsed.path)).expanduser(),
            )

        if scheme not in {"neo4j", "neo4j+ssc", "bolt+s"}:
            raise ValueError(
                f"unsupported storage URL scheme {scheme!r}; "
                "expected networkx, neo4j, neo4j+ssc, or bolt+s"
            )

        if parsed.query or parsed.fragment:
            raise ValueError("Neo4j storage URL must not include query or fragment")

        if parsed.hostname is None:
            raise ValueError("Neo4j storage URL requires a host")

        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Neo4j storage URL has an invalid port") from exc

        database = unquote(parsed.path.lstrip("/"))
        if not database or "/" in database:
            raise ValueError("Neo4j storage URL requires exactly one database name")

        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"

        authority = host if port is None else f"{host}:{port}"
        neo4j_uri = f"{scheme}://{authority}"

        return cls(
            backend="neo4j",
            neo4j_uri=neo4j_uri,
            neo4j_user=unquote(parsed.username) if parsed.username is not None else None,
            neo4j_password=(unquote(parsed.password) if parsed.password is not None else None),
            neo4j_database=database,
        )


@dataclass(frozen=True)
class RuntimeConfig:
    storage_backend: str
    storage_path: Path | None
    neo4j_uri: str | None
    neo4j_user: str
    neo4j_password: str | None = field(repr=False)
    neo4j_database: str
    audit_db_url: str
    audit_signing_key_env: str
    listen_host: str
    listen_port: int
    ontology_path: Path | None

    def with_storage_override(self, storage: StorageConfig) -> RuntimeConfig:
        """Return this runtime config with only storage settings overridden.

        CLI storage URL values win. Existing environment-derived values fill
        Neo4j credential gaps when the URL omits them. Non-storage runtime
        settings remain unchanged.
        """
        if storage.backend == "networkx":
            return replace(
                self,
                storage_backend="networkx",
                storage_path=storage.path,
            )

        if storage.backend == "neo4j":
            return replace(
                self,
                storage_backend="neo4j",
                storage_path=None,
                neo4j_uri=storage.neo4j_uri or self.neo4j_uri,
                neo4j_user=storage.neo4j_user or self.neo4j_user,
                neo4j_password=(
                    storage.neo4j_password
                    if storage.neo4j_password is not None
                    else self.neo4j_password
                ),
                neo4j_database=storage.neo4j_database or self.neo4j_database,
            )

        raise ValueError(f"unsupported storage backend {storage.backend!r}")

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
            neo4j_uri=os.getenv("NEO4J_URI"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD"),
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
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
