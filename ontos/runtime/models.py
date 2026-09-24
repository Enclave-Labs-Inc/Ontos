"""Load-bearing data models.

Every model here is designed so that its shape survives Neo4j / Neptune / LadybugDB
storage. `Fact` is deliberately immutable — supersession creates a new Fact and links
back via `superseded_by`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Confidence(StrEnum):
    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


class Provenance(BaseModel):
    """Per-fact provenance chain. Required on every Fact."""

    source_id: str
    extractor_id: str
    extractor_version: str
    confidence: Confidence
    confidence_score: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(frozen=True)


class Fact(BaseModel):
    """Immutable triple with bitemporal validity and provenance.

    Never mutate; to correct a fact, write a new Fact with `superseded_by` set
    to the old one's id and close the old fact's validity window via storage.
    """

    id: UUID = Field(default_factory=uuid4)
    subject_id: str
    predicate: str
    object_id: str
    provenance: Provenance
    t_valid: datetime
    t_invalid: datetime | None = None
    ingested_at: datetime
    superseded_by: UUID | None = None
    acl_ref: str | None = None

    model_config = ConfigDict(frozen=True)


class Result(BaseModel):
    """A single search/traverse hit with the provenance chain that produced it."""

    fact: Fact
    score: float = Field(ge=0.0, le=1.0)
    path: list[UUID] = Field(default_factory=list)


class ArticleTwelveField(StrEnum):
    """The 12 fields required per AI-influenced decision by EU AI Act Article 12."""

    QUERY_ID = "query_id"
    AGENT_IDENTITY = "agent_identity"
    ACTING_ON_BEHALF_OF = "acting_on_behalf_of"
    QUERY_TEXT = "query_text"
    TOOL_INVOKED = "tool_invoked"
    TOOL_ARGUMENTS = "tool_arguments"
    RESULT_FACT_IDS = "result_fact_ids"
    RESULT_HASH = "result_hash"
    TIMESTAMP = "timestamp"
    LATENCY_MS = "latency_ms"
    MODEL_VERSIONS = "model_versions"
    POLICY_DECISIONS = "policy_decisions"


class AuditRecord(BaseModel):
    """Hash-chained audit event.

    `prev_hash` links to the previous record's `hash`, forming an append-only
    tamper-evident log. Article-12 fields are captured under `article12`.
    """

    query_id: UUID
    timestamp: datetime
    article12: dict[ArticleTwelveField, str]
    prev_hash: str | None
    hash: str

    model_config = ConfigDict(frozen=True)


ToolName = Literal["search", "traverse", "explain", "provenance", "audit", "as_of"]
