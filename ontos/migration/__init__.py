"""Lazy ontology migration at query time.

#34 — Facts are stamped at ingest with the ontology they were extracted
under (`Provenance.ontology_id` / `Provenance.ontology_version`). When
the active ontology evolves — predicate rename, deprecation,
cardinality tighten — historical facts would otherwise become
unreachable or incoherent. `MigrationRegistry` translates them forward
on read, keeping the stored facts bitemporally immutable.

The seam mirrors `SupersessionPolicy`: a Protocol with `id`,
`from_version`, `to_version`, and an async per-fact decide method.
Operators register migrations at construction time; the registry
chains them via BFS when the target is more than one hop away.
"""

from ontos.migration.base import MigrationWarning, OntologyMigration
from ontos.migration.builtins import (
    CardinalityTighten,
    PredicateDeprecate,
    PredicateRename,
)
from ontos.migration.registry import MigrationRegistry

__all__ = [
    "CardinalityTighten",
    "MigrationRegistry",
    "MigrationWarning",
    "OntologyMigration",
    "PredicateDeprecate",
    "PredicateRename",
]
