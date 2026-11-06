"""Verify declarative migrations against immutable ontology history.

An identity migration is safe only when the target schema is exactly
the result of applying its recorded additive changes to the source.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ontos.migration.records import MigrationRecord
from ontos.ontology.loader import Ontology, load_ontology
from ontos.ontology.versioning import (
    OntologyVersionError,
    build_next_ontology,
)


class MigrationHistoryError(ValueError):
    """Historical ontology versions do not substantiate a migration."""


def verify_migration_history(
    *,
    records: Sequence[MigrationRecord],
    historical_ontology_dir: Path | str,
    active_ontology: Ontology,
) -> None:
    """Verify every identity edge against historical ontology YAML.

    The directory contains earlier ontology versions. The active
    ontology is supplied separately and must not be duplicated there.

    Equality is intentionally strict: unrecorded schema modifications,
    including removals, cannot be legitimized by an identity migration.
    """
    root = Path(historical_ontology_dir)

    if not root.is_dir():
        raise MigrationHistoryError(f"historical ontology directory does not exist: {root}")

    files = sorted(
        (*root.glob("*.yaml"), *root.glob("*.yml")),
        key=lambda path: path.name,
    )

    if not files:
        raise MigrationHistoryError(f"no historical ontology versions found in {root}")

    versions: dict[str, Ontology] = {active_ontology.version: active_ontology}

    for path in files:
        try:
            ontology = load_ontology(path)
        except (OSError, ValueError, NotImplementedError) as exc:
            raise MigrationHistoryError(f"invalid historical ontology: {path}") from exc

        if ontology.id != active_ontology.id:
            raise MigrationHistoryError(f"historical ontology family mismatch: {path}")

        if ontology.version in versions:
            raise MigrationHistoryError(
                f"duplicate historical ontology version: {ontology.version!r}"
            )

        versions[ontology.version] = ontology

    for record in records:
        if record.ontology_id != active_ontology.id:
            raise MigrationHistoryError("migration record belongs to a different ontology")

        source = versions.get(record.from_version)
        target = versions.get(record.to_version)

        if source is None:
            raise MigrationHistoryError(
                f"missing historical source ontology version {record.from_version!r}"
            )

        if target is None:
            raise MigrationHistoryError(
                f"missing historical target ontology version {record.to_version!r}"
            )

        try:
            expected = build_next_ontology(source, record)
        except (OntologyVersionError, ValueError) as exc:
            raise MigrationHistoryError(
                f"invalid recorded ontology transition "
                f"{record.from_version!r} -> {record.to_version!r}"
            ) from exc

        if expected != target:
            raise MigrationHistoryError(
                f"ontology transition {record.from_version!r} -> "
                f"{record.to_version!r} contains unrecorded changes"
            )
