"""Construct a new ontology version from a declarative additive migration.

This module does not write files, activate an ontology, or modify facts.
"""

from __future__ import annotations

from ontos.migration.records import (
    AddPatternChange,
    MapToExistingChange,
    MigrationRecord,
    NewRelationTypeChange,
)
from ontos.ontology.loader import (
    Ontology,
    Pattern,
    PredicateMapping,
    RelationType,
)


class OntologyVersionError(ValueError):
    """A requested ontology version transition is invalid."""


def build_next_ontology(
    base: Ontology,
    record: MigrationRecord,
) -> Ontology:
    """Apply an approved additive change to a copy of the base ontology.

    The caller must separately authorize the record and safely persist
    the resulting version. This function only builds and validates it.
    """
    if record.ontology_id != base.id:
        raise OntologyVersionError("migration ontology_id does not match the base ontology")

    if record.from_version != base.version:
        raise OntologyVersionError("stale migration: from_version does not match the base ontology")

    entity_types = list(base.entity_types)
    relation_types = list(base.relation_types)
    patterns = list(base.patterns)
    predicate_mappings = list(base.predicate_mappings)

    for change in record.approved_changes:
        if isinstance(change, AddPatternChange):
            pattern = Pattern(
                subject_type=change.subject_type,
                predicate=change.predicate,
                object_type=change.object_type,
            )
            if pattern in patterns:
                raise OntologyVersionError("pattern already exists")
            patterns.append(pattern)

        elif isinstance(change, MapToExistingChange):
            mapping = PredicateMapping(
                subject_type=change.subject_type,
                observed_predicate=change.observed_predicate,
                object_type=change.object_type,
                maps_to=change.maps_to,
            )

            key = (
                mapping.subject_type,
                mapping.observed_predicate,
                mapping.object_type,
            )
            if any(
                (existing.subject_type, existing.observed_predicate, existing.object_type) == key
                for existing in predicate_mappings
            ):
                raise OntologyVersionError("predicate mapping already exists")

            predicate_mappings.append(mapping)

        elif isinstance(change, NewRelationTypeChange):
            if any(rt.label == change.relation_type for rt in relation_types):
                raise OntologyVersionError("relation type already exists")

            relation_types.append(
                RelationType(
                    label=change.relation_type,
                    description=change.description,
                    cardinality=change.cardinality,
                )
            )

            pattern = Pattern(
                subject_type=change.subject_type,
                predicate=change.relation_type,
                object_type=change.object_type,
            )
            if pattern in patterns:
                raise OntologyVersionError("pattern already exists")

            patterns.append(pattern)

    updated = Ontology(
        id=base.id,
        version=record.to_version,
        entity_types=entity_types,
        relation_types=relation_types,
        patterns=patterns,
        predicate_mappings=predicate_mappings,
    )

    problems = updated.check_referential_integrity()
    if problems:
        raise OntologyVersionError("invalid resulting ontology: " + "; ".join(problems))

    return updated
