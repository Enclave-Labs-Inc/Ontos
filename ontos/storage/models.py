"""Backend-neutral read models for graph export.

These models describe the persisted graph surface exposed to exporters.
They intentionally do not reuse runtime ``Entity`` because stores persist
only a subset of entity provenance plus storage-level ACL metadata.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Fact


class StoredEntity(BaseModel):
    """Entity attributes reconstructed from the storage layer."""

    id: str
    type: str | None = None
    canonical_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
    provenance_source_id: str | None = None
    provenance_extractor_id: str | None = None
    acl_ref: str | None = None

    model_config = ConfigDict(frozen=True)


class GraphScan(BaseModel):
    """Backend-neutral snapshot consumed by graph exporters."""

    entities: list[StoredEntity] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    merges: list[MergeRecord] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)
