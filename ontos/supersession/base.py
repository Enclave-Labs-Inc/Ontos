"""SupersessionPolicy Protocol + decision records.

A supersession policy inspects a newly-ingested fact against the store
and decides whether any older active facts should be closed with
`t_invalid` and `superseded_by` set to the new fact. The pluggable
seam mirrors `Resolver` so a regulated buyer can plug in stricter
rules without a core edit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ontos.ontology import Ontology
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore


class SupersessionRecord(BaseModel):
    """Audit breadcrumb for one supersession decision.

    Regulators tracing an answer can walk this log to reconstruct
    which prior facts a newer one closed and when.
    """

    new_fact_id: UUID
    closed_fact_ids: list[UUID]
    policy_id: str
    policy_version: str
    decided_at: datetime
    reason: str = ""

    model_config = ConfigDict(frozen=True)


class SupersessionDecision(BaseModel):
    """What a SupersessionPolicy returns per new fact.

    - `facts_to_close`: pre-existing active facts the pipeline must
      close via `store.close_fact(...)` AFTER adding the new fact.
    - `skip_add`: if True, the new fact is a re-affirmation of a
      currently-active identical fact and must NOT be written.
    - `record`: the audit breadcrumb (None when no supersession fires).
    """

    facts_to_close: list[Fact] = Field(default_factory=list)
    skip_add: bool = False
    record: SupersessionRecord | None = None

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class SupersessionPolicy(Protocol):
    """Every supersession implementation satisfies this surface."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def decide(
        self,
        new_fact: Fact,
        store: GraphStore,
        ontology: Ontology,
    ) -> SupersessionDecision: ...


class NullSupersessionPolicy:
    """Explicit opt-out — never closes anything.

    Operators who deliberately want to stack every ingest (e.g. a
    forensic-ingest pipeline that preserves every raw claim exactly
    as asserted) pass this instead of leaving `supersession=None`,
    so the opt-out is visible in constructor arguments and audit.
    """

    id: str = "ontos.supersession.null"
    version: str = "0.1.0"

    async def decide(
        self,
        new_fact: Fact,
        store: GraphStore,
        ontology: Ontology,
    ) -> SupersessionDecision:
        return SupersessionDecision()
