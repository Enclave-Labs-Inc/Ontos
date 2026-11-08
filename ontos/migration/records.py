"""Declarative, versioned ontology migration records for M3.

An additive approval creates an identity migration edge. It never
rewrites stored facts or promotes rejected candidates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ontos.migration.builtins import IdentityMigration
from ontos.migration.registry import MigrationRegistry
from ontos.ontology.loader import Cardinality


class AddPatternChange(BaseModel):
    action: Literal["add_pattern"]
    subject_type: str
    predicate: str
    object_type: str

    model_config = ConfigDict(frozen=True, extra="forbid")


class MapToExistingChange(BaseModel):
    action: Literal["map_to_existing"]
    subject_type: str
    observed_predicate: str
    object_type: str
    maps_to: str

    model_config = ConfigDict(frozen=True, extra="forbid")


class NewRelationTypeChange(BaseModel):
    action: Literal["new_relation_type"]
    subject_type: str
    relation_type: str
    object_type: str
    description: str = ""
    cardinality: Cardinality = "many_to_many"

    model_config = ConfigDict(frozen=True, extra="forbid")


ApprovedChange = Annotated[
    AddPatternChange | MapToExistingChange | NewRelationTypeChange,
    Field(discriminator="action"),
]


class MigrationRecord(BaseModel):
    """A reviewed old-to-new ontology version transition."""

    schema_version: Literal[1]
    ontology_id: str = Field(min_length=1)
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)
    migration_kind: Literal["identity"]
    approval_query_id: UUID
    approved_changes: list[ApprovedChange] = Field(min_length=1)
    # Optional at parse time for historical fixtures; verified reads reject absence.
    approval_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("ontology_id", "from_version", "to_version")
    @classmethod
    def _nonblank_identifier(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("identifier must be nonblank and have no surrounding spaces")
        return value

    @model_validator(mode="after")
    def _distinct_versions(self) -> Self:
        if self.from_version == self.to_version:
            raise ValueError("migration versions must differ")
        return self


class MigrationRecordLoadError(ValueError):
    """Invalid or unreadable declarative migration record."""


def load_migration_record(path: Path | str) -> MigrationRecord:
    """Load and validate one migration YAML file without executing code."""
    location = Path(path)

    try:
        payload = yaml.safe_load(location.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MigrationRecordLoadError(
            f"failed to load migration record {location}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise MigrationRecordLoadError(f"migration record {location} must be a YAML mapping")

    try:
        return MigrationRecord.model_validate(payload)
    except ValidationError as exc:
        raise MigrationRecordLoadError(f"invalid migration record {location}: {exc}") from exc


def register_migration_record(
    registry: MigrationRegistry,
    record: MigrationRecord,
) -> None:
    """Register a validated additive migration without touching stored facts."""
    registry.register(
        IdentityMigration(
            from_version=record.from_version,
            to_version=record.to_version,
            approval_query_id=record.approval_query_id,
        )
    )
