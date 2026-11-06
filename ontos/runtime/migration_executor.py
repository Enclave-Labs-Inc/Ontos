"""Build a deterministic executor with verified ontology migrations.

Both CLI and MCP serving can use this factory. Configured migrations
are strictly verified before any query can execute.
"""

from __future__ import annotations

from ontos.executor import DeterministicExecutor
from ontos.migration.loading import (
    MigrationConfigurationError,
    load_migration_registry,
)
from ontos.ontology import Ontology, load_ontology
from ontos.runtime.config import RuntimeConfig


def build_verified_executor(
    config: RuntimeConfig,
    *,
    active_ontology: Ontology | None = None,
) -> DeterministicExecutor:
    """Create an executor with verified strict migrations when configured.

    An explicitly provided ontology takes precedence over the configured
    ontology path, allowing local CLI --ontology overrides.

    With no migration configuration, legacy executor behavior is retained.
    """
    migration_dir = config.migration_dir
    history_dir = config.ontology_history_dir

    if migration_dir is None and history_dir is None:
        return DeterministicExecutor()

    if migration_dir is None or history_dir is None:
        raise MigrationConfigurationError(
            "both migration and ontology-history directories are required"
        )

    if active_ontology is None:
        if config.ontology_path is None:
            raise MigrationConfigurationError(
                "an active ontology is required when migrations are configured"
            )
        active_ontology = load_ontology(config.ontology_path)

    registry = load_migration_registry(
        migration_dir,
        active_ontology=active_ontology,
        historical_ontology_dir=history_dir,
        strict=True,
    )

    return DeterministicExecutor(
        current_ontology=active_ontology,
        migration_registry=registry,
    )
