"""Backend-agnostic graph store interface.

Every tool and executor accesses storage through this Protocol — never
call the underlying driver directly. Storage stays authz-agnostic; the
server tools resolve the caller's identity into a concrete
`allowed_acls` list via `ontos.authz.AuthzBackend` once per request and
pass it in.

ACL semantics on read methods:

- `acl_ref is None` on a fact means "public" — always visible.
- `allowed_acls is not None` (M2) is the authz-resolved path — a fact
  is visible iff `fact.acl_ref in allowed_acls`.
- `acl_subject is not None` and `allowed_acls is None` (M1 shim,
  dev-only) — a fact is visible iff `fact.acl_ref == acl_subject`.
- Both `None` and `acl_ref is set` — denied. Deny-by-default when a
  fact declares an ACL and no caller identity is supplied.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal, Protocol
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
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]: ...

    async def traverse(
        self,
        start: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
        depth: int = 2,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> Iterable[Fact]: ...

    async def facts_for_entity(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]: ...

    async def close(self) -> None: ...


class StorageError(RuntimeError):
    """Raised when a store cannot load, persist, or recover state.

    Used by backends that persist to a local file (e.g. the dev-only
    pickle format of ``NetworkxStore``) to fail loud on magic-header
    mismatch, corrupt payloads, or version drift. Fail-loud is the
    right posture here: silent reset of persisted facts would quietly
    violate the provenance invariant.
    """
