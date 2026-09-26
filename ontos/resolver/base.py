"""Resolver Protocol + MergeRecord.

Entity resolution deduplicates candidate `Entity` records that refer
to the same real-world thing. In an enterprise fabric this happens
across many source systems (`user:alice` in Slack ≡ `usr_42` in
Postgres ≡ `alice@corp.com` in email) and it's where most enterprise
KGs quietly fail — the M1.d design called it out explicitly.

The cascade approach (Rules → ML → LLM) is the industry consensus per
the Round-2 research: cheap deterministic rules first, statistical
models for the middle tier, and an LLM only for the ambiguous long
tail. Each tier hands the survivors to the next.

Every merge produces a `MergeRecord` so audit can reconstruct which
original entity ids merged into a canonical entity — the compliance
requirement M1.d's design pinned as a hard requirement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ontos.runtime.models import Entity


class MergeRecord(BaseModel):
    """Audit breadcrumb for one merge decision.

    Written by every resolver whenever two candidate entities collapse
    into one canonical. Regulators tracing an answer can walk this
    log to see which original records were consolidated.
    """

    canonical_id: str
    merged_ids: list[str]
    resolver_id: str
    resolver_version: str
    resolved_at: datetime
    reason: str = ""

    model_config = ConfigDict(frozen=True)


class ResolutionResult(BaseModel):
    """What a Resolver returns.

    `entities` is the deduped output; `merges` records every
    consolidation decision the resolver made; `unresolved` is the
    subset that fell through — the cascade hands these to the next
    tier.
    """

    entities: list[Entity] = Field(default_factory=list)
    merges: list[MergeRecord] = Field(default_factory=list)
    unresolved: list[Entity] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class Resolver(Protocol):
    """Every resolver tier implements this."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def resolve(self, candidates: list[Entity]) -> ResolutionResult: ...


class ResolverError(RuntimeError):
    """Raised when a resolver tier fails structurally (never for 'no match')."""
