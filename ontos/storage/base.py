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

from ontos.runtime.models import Entity, Fact


class GraphStore(Protocol):
    """Storage contract.

    Writes are always additive (`add_fact`) or supersession-based
    (`close_fact`). No in-place mutation.
    """

    async def add_fact(self, fact: Fact) -> None: ...

    async def upsert_entity(self, entity: Entity, *, acl_ref: str | None = None) -> None:
        """Idempotent entity write.

        Stores ``type``, ``canonical_name``, ``aliases``,
        ``properties``, a provenance pointer, and an optional
        ``acl_ref`` on the entity node. Last-write-wins on repeated
        upserts for every attribute **except** ``acl_ref``, which is
        first-write-wins: an entity first stamped by a restricted
        document stays restricted even when a later public ingest
        re-upserts it. Operators who need to broaden an entity's ACL
        use a dedicated admin path, not routine ingest.

        Entity properties are not bitemporal facts and have no
        supersession policy. Callers that need change history use
        the resolver's ``MergeRecord`` audit trail, not this seam.

        MUST be MERGE-based: re-ingesting the same entity never
        duplicates nodes. ``IngestPipeline.run`` calls this before
        ``add_fact`` so every ``Fact.subject_id`` / ``Fact.object_id``
        has a backing typed node by the time the fact lands.

        **Read-side ACL contract (CLAUDE.md invariant):** backends
        that persist entity attrs (name, aliases, properties) MUST
        filter at read time on ``acl_ref`` the same way ``search`` /
        ``traverse`` / ``facts_for_entity`` already do for facts.
        Returning an entity's attrs to a caller who can't see any
        fact citing it would leak the entity's existence — the
        permission-aware-traversal invariant forbids that.
        """
        ...

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
