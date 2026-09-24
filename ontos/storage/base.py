"""Backend-agnostic graph store interface.

Every tool and executor accesses storage through this Protocol — never
call the underlying driver directly. This lets M1 swap in Neo4j without
touching runtime code.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ontos.runtime.models import Fact


class GraphStore(Protocol):
    """Storage contract.

    Writes are always additive (`add_fact`) or supersession-based
    (`close_fact`). No in-place mutation.
    """

    async def add_fact(self, fact: Fact) -> None: ...

    async def close_fact(
        self,
        fact_id: UUID,
        t_invalid: datetime,
        superseded_by: UUID | None = None,
    ) -> None: ...

    async def get_fact(self, fact_id: UUID) -> Fact | None: ...

    async def search(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        k: int = 10,
        acl_subject: str | None = None,
    ) -> list[Fact]: ...

    async def traverse(
        self,
        start: str,
        *,
        relation: str | None = None,
        depth: int = 2,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
    ) -> Iterable[Fact]: ...

    async def facts_for_entity(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
    ) -> list[Fact]: ...

    async def close(self) -> None: ...
