"""Load and validate an ontology from YAML.

The `Ontology` model is deliberately small — it captures only what the
planner (M3), extractor (M1.d), and pruner need at runtime:

- entity types with their properties (used to constrain extraction and
  validate incoming facts)
- relation types with cardinality (planner needs this to reject
  semantically-impossible queries per CypherBench's error taxonomy)
- patterns — triplets of `(subject_type, predicate, object_type)` that
  say which directed relations are allowed

The YAML format is our own — small, obvious, easy to hand-edit. A
future milestone will add a LinkML loader that maps a LinkML
`SchemaDefinition` into the same `Ontology` model (the `.linkml.yaml`
suffix routes there); for M1.c calling `load_ontology()` on a
LinkML-shaped file raises a `NotImplementedError` with a clear pointer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ontos.ontology.labels import is_relation_label

# The typed property values we can persist onto entities and relations
# via Neo4j's native driver types. Anything richer belongs in a Fact,
# not a schema property.
PropertyType = Literal["STRING", "INTEGER", "FLOAT", "BOOLEAN", "DATE", "DATETIME"]

# Cardinality drives the planner's semantic check (CypherBench's
# "reversed direction" and "pattern misalignment" failure modes both
# lean on the cardinality of the target relation).
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class PropertyDef(BaseModel):
    name: str
    type: PropertyType
    required: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("name")
    @classmethod
    def _reject_reserved_names(cls, v: str) -> str:
        # `id` collides with the entity id and the Neo4j element id;
        # `fact_id` collides with the fact edge property. Rejecting
        # them at load time surfaces the mistake before ingestion.
        if v in {"id", "fact_id", "predicate"}:
            raise ValueError(
                f"property name {v!r} is reserved — use a domain-specific name instead"
            )
        return v


class EntityType(BaseModel):
    label: str
    description: str = ""
    properties: list[PropertyDef] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class RelationType(BaseModel):
    label: str
    description: str = ""
    properties: list[PropertyDef] = Field(default_factory=list)
    cardinality: Cardinality = "many_to_many"

    model_config = ConfigDict(frozen=True)


class Pattern(BaseModel):
    """A directed allowed edge: `subject_type --predicate-> object_type`."""

    subject_type: str
    predicate: str
    object_type: str

    model_config = ConfigDict(frozen=True)


class PredicateMapping(BaseModel):
    """Normalize an observed relation for one directed type pair."""

    subject_type: str
    observed_predicate: str
    object_type: str
    maps_to: str

    model_config = ConfigDict(frozen=True)

    @field_validator("observed_predicate")
    @classmethod
    def _valid_observed_predicate(cls, value: str) -> str:
        if not is_relation_label(value):
            raise ValueError("observed_predicate must be lower_snake_case, max 64 characters")
        return value


class Ontology(BaseModel):
    # #34: `id` + `version` form the stamp written into
    # `Provenance.ontology_id` / `Provenance.ontology_version` on every
    # fact (and entity) extracted under this ontology. Readers rely on
    # that stamp to migrate historical facts forward at query time via
    # `ontos.migration.MigrationRegistry`.
    #
    # `id` is a stable, dotted identifier (e.g. "ontos.starter",
    # "customer.finops"); never rename once facts have been written
    # under it. The `"ontos.unknown"` default lets pre-#34 YAMLs
    # (no top-level `id:` key) keep loading — facts extracted under
    # one show up with that sentinel so operators notice.
    id: str = "ontos.unknown"
    version: str = "0.1"
    entity_types: list[EntityType]
    relation_types: list[RelationType]
    patterns: list[Pattern]
    predicate_mappings: list[PredicateMapping] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)

    @field_validator("entity_types")
    @classmethod
    def _entity_types_non_empty(cls, v: list[EntityType]) -> list[EntityType]:
        if not v:
            raise ValueError("ontology must declare at least one entity_type")
        return v

    @field_validator("relation_types")
    @classmethod
    def _relation_types_non_empty(cls, v: list[RelationType]) -> list[RelationType]:
        if not v:
            raise ValueError("ontology must declare at least one relation_type")
        return v

    def entity_type(self, label: str) -> EntityType | None:
        for et in self.entity_types:
            if et.label == label:
                return et
        return None

    def relation_type(self, label: str) -> RelationType | None:
        for rt in self.relation_types:
            if rt.label == label:
                return rt
        return None

    def is_valid_pattern(self, subject_type: str, predicate: str, object_type: str) -> bool:
        for p in self.patterns:
            if (
                p.subject_type == subject_type
                and p.predicate == predicate
                and p.object_type == object_type
            ):
                return True
        return False

    def resolve_predicate(
        self,
        subject_type: str,
        observed_predicate: str,
        object_type: str,
    ) -> str:
        """Resolve a mapping only for its exact directed type pair."""
        for mapping in self.predicate_mappings:
            if (
                mapping.subject_type == subject_type
                and mapping.observed_predicate == observed_predicate
                and mapping.object_type == object_type
            ):
                return mapping.maps_to
        return observed_predicate

    def check_referential_integrity(self) -> list[str]:
        """Return a list of human-readable problems, empty if the ontology is coherent."""
        problems: list[str] = []
        entity_labels = {et.label for et in self.entity_types}
        relation_labels = {rt.label for rt in self.relation_types}
        for i, p in enumerate(self.patterns):
            if p.subject_type not in entity_labels:
                problems.append(
                    f"pattern[{i}]: subject_type {p.subject_type!r} is not a declared entity_type"
                )
            if p.object_type not in entity_labels:
                problems.append(
                    f"pattern[{i}]: object_type {p.object_type!r} is not a declared entity_type"
                )
            if p.predicate not in relation_labels:
                problems.append(
                    f"pattern[{i}]: predicate {p.predicate!r} is not a declared relation_type"
                )
        seen_mappings: set[tuple[str, str, str]] = set()

        for i, mapping in enumerate(self.predicate_mappings):
            key = (
                mapping.subject_type,
                mapping.observed_predicate,
                mapping.object_type,
            )

            if key in seen_mappings:
                problems.append(f"predicate_mappings[{i}]: duplicate mapping for {key!r}")
            seen_mappings.add(key)

            if mapping.subject_type not in entity_labels:
                problems.append(
                    f"predicate_mappings[{i}]: unknown subject_type {mapping.subject_type!r}"
                )

            if mapping.object_type not in entity_labels:
                problems.append(
                    f"predicate_mappings[{i}]: unknown object_type {mapping.object_type!r}"
                )

            if mapping.maps_to not in relation_labels:
                problems.append(
                    f"predicate_mappings[{i}]: unknown target relation {mapping.maps_to!r}"
                )
            elif not self.is_valid_pattern(
                mapping.subject_type,
                mapping.maps_to,
                mapping.object_type,
            ):
                problems.append(
                    f"predicate_mappings[{i}]: target relation is not valid for the type pair"
                )

            if self.is_valid_pattern(
                mapping.subject_type,
                mapping.observed_predicate,
                mapping.object_type,
            ):
                problems.append(
                    f"predicate_mappings[{i}]: observed predicate is already a valid pattern"
                )
        return problems


class OntologyLoadError(ValueError):
    """Raised when an ontology file is malformed."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"failed to load ontology {path}: {message}")


def load_ontology(path: Path | str) -> Ontology:
    """Load an ontology from a YAML file.

    LinkML `.linkml.yaml` files are recognized but currently raise
    `NotImplementedError` — the LinkML loader lands in M2 alongside
    stricter schema enforcement.
    """
    p = Path(path)
    if not p.exists():
        raise OntologyLoadError(p, "file not found")

    if p.name.endswith(".linkml.yaml") or p.name.endswith(".linkml.yml"):
        raise NotImplementedError(
            "LinkML loader is planned for M2 — use the native ontos YAML "
            "format for now. See docs/ontology/examples/ for the shape."
        )

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise OntologyLoadError(p, f"invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise OntologyLoadError(
            p,
            "top-level YAML value must be a mapping with keys "
            "'entity_types', 'relation_types', 'patterns'",
        )

    try:
        ontology = Ontology(**raw)
    except Exception as exc:  # pydantic ValidationError et al.
        raise OntologyLoadError(p, str(exc)) from exc

    problems = ontology.check_referential_integrity()
    if problems:
        raise OntologyLoadError(p, "referential integrity failed:\n  - " + "\n  - ".join(problems))

    return ontology
