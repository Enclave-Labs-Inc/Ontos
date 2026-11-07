"""Load a single ontology family's declarative migration chain.

The directory contains operator-managed migration records. Each edge
must eventually lead to the active ontology version. Historical
ontology-file verification is mandatory before registry construction.
"""

from __future__ import annotations

from pathlib import Path

from ontos.audit.base import AuditSink
from ontos.migration.approval_audit import verify_migration_approval_records
from ontos.migration.history import MigrationHistoryError, verify_migration_history
from ontos.migration.records import (
    MigrationRecord,
    MigrationRecordLoadError,
    load_migration_record,
    register_migration_record,
)
from ontos.migration.registry import MigrationRegistry
from ontos.ontology.loader import Ontology


class MigrationConfigurationError(ValueError):
    """Configured migration records are missing or inconsistent."""


def _load_migration_registry_with_records(
    directory: Path | str,
    *,
    active_ontology: Ontology,
    historical_ontology_dir: Path | str,
    strict: bool = True,
) -> tuple[MigrationRegistry, list[MigrationRecord]]:
    """Load a complete, non-branching chain ending at the active version.

    Validate version connectivity and every recorded transition
    against historical ontology files before registering edges.
    """
    root = Path(directory)

    if not root.is_dir():
        raise MigrationConfigurationError(f"migration directory does not exist: {root}")

    files = sorted(
        (*root.glob("*.yaml"), *root.glob("*.yml")),
        key=lambda path: path.name,
    )

    if not files:
        raise MigrationConfigurationError(f"no migration records found in {root}")

    records: list[MigrationRecord] = []
    by_source: dict[str, MigrationRecord] = {}

    for path in files:
        try:
            record = load_migration_record(path)
        except MigrationRecordLoadError as exc:
            raise MigrationConfigurationError(f"invalid migration configuration: {path}") from exc

        if record.ontology_id != active_ontology.id:
            raise MigrationConfigurationError(f"migration ontology mismatch: {path}")

        if record.from_version in by_source:
            raise MigrationConfigurationError(
                f"duplicate or conflicting migration from version {record.from_version!r}"
            )

        by_source[record.from_version] = record
        records.append(record)

    # The active ontology must be the terminal version. Otherwise a
    # cycle that returns to the active version can pass reachability
    # checks even though the migration graph is circular.
    if active_ontology.version in by_source:
        raise MigrationConfigurationError(
            f"active ontology version {active_ontology.version!r} "
            "must be terminal in the migration chain"
        )

    # A complete activated lineage has exactly one forward route
    # from every historical edge to the active ontology version.
    for record in records:
        current = record.to_version
        visited = {record.from_version}

        while current != active_ontology.version:
            if current in visited:
                raise MigrationConfigurationError(
                    f"cycle in migration chain at version {current!r}"
                )

            visited.add(current)
            successor = by_source.get(current)

            if successor is None:
                raise MigrationConfigurationError(
                    f"migration chain from {record.from_version!r} "
                    f"does not reach active version {active_ontology.version!r}"
                )

            current = successor.to_version

    # Do not trust an identity edge until the actual historical
    # ontology files prove that all recorded changes were additive.
    try:
        verify_migration_history(
            records=records,
            historical_ontology_dir=historical_ontology_dir,
            active_ontology=active_ontology,
        )
    except MigrationHistoryError as exc:
        raise MigrationConfigurationError(f"migration history verification failed: {exc}") from exc

    registry = MigrationRegistry(strict=strict)

    for record in records:
        register_migration_record(registry, record)

    return registry, records


def load_migration_registry(
    directory: Path | str,
    *,
    active_ontology: Ontology,
    historical_ontology_dir: Path | str,
    strict: bool = True,
) -> MigrationRegistry:
    """Legacy synchronous loader retained during runtime integration.

    This validates migration history but NOT Article-12 approvals.
    Production paths must use load_audit_verified_migration_registry.
    """
    registry, _ = _load_migration_registry_with_records(
        directory,
        active_ontology=active_ontology,
        historical_ontology_dir=historical_ontology_dir,
        strict=strict,
    )
    return registry


async def load_audit_verified_migration_registry(
    directory: Path | str,
    *,
    active_ontology: Ontology,
    historical_ontology_dir: Path | str,
    audit: AuditSink,
    strict: bool = True,
) -> MigrationRegistry:
    """Require valid history and matching Article-12 approvals.

    Verification uses precisely the records from registry construction.
    Nothing is returned to callers when verification fails.
    """
    registry, records = _load_migration_registry_with_records(
        directory,
        active_ontology=active_ontology,
        historical_ontology_dir=historical_ontology_dir,
        strict=strict,
    )
    await verify_migration_approval_records(
        records=records,
        audit=audit,
    )
    return registry
