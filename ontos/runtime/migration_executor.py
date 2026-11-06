"""Build a deterministic executor with verified ontology migrations.

Both CLI and MCP serving can use this factory. Configured migrations
are strictly verified before any query can execute.
"""

from __future__ import annotations

from ontos.executor import DeterministicExecutor
from ontos.ontology import Ontology
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.migration_view import MigrationView, build_migration_view


def build_verified_executor(
    config: RuntimeConfig,
    *,
    active_ontology: Ontology | None = None,
    view: MigrationView | None = None,
) -> DeterministicExecutor:
    """Create an executor with verified strict migrations when configured.

    An explicitly provided ontology takes precedence over the configured
    ontology path, allowing local CLI --ontology overrides. A `view` already
    built by the caller is reused so every read path shares one registry.

    With no migration configuration, legacy executor behavior is retained.
    """
    if view is None:
        view = build_migration_view(config, active_ontology=active_ontology)
    if view is None:
        return DeterministicExecutor()
    return DeterministicExecutor(
        current_ontology=view.ontology,
        migration_registry=view.registry,
    )
