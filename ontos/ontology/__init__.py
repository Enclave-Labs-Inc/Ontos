"""ontology subsystem for ontos."""

from ontos.ontology.loader import (
    Cardinality,
    EntityType,
    Ontology,
    OntologyLoadError,
    Pattern,
    PropertyDef,
    PropertyType,
    RelationType,
    load_ontology,
)

__all__ = [
    "Cardinality",
    "EntityType",
    "Ontology",
    "OntologyLoadError",
    "Pattern",
    "PropertyDef",
    "PropertyType",
    "RelationType",
    "load_ontology",
]
