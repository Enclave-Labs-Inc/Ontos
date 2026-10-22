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
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from ontos.extraction.base import RejectedCandidate
from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Entity, Fact
from ontos.storage.models import GraphScan, StoredEntity


@dataclass(frozen=True)
class StoredCandidate:
    """Rejected candidate together with its physical storage ID."""

    candidate: RejectedCandidate
    stored_id: str


def candidate_storage_id(candidate: RejectedCandidate) -> str:
    """Stable identity for an ACL-scoped rejected candidate."""
    import hashlib
    import json

    # Stable logical identity, independent of confidence fluctuations.
    # ACL scope is part of identity to prevent cross-permission merging.
    triple = candidate.raw_triple
    provenance = candidate.provenance

    identity = {
        "source_id": provenance.source_id,
        "subject_id": triple.subject_id,
        "subject_type": triple.subject_type,
        "subject_canonical_name": triple.subject_canonical_name,
        "predicate": triple.predicate,
        "object_id": triple.object_id,
        "object_type": triple.object_type,
        "object_canonical_name": triple.object_canonical_name,
        "reason": candidate.reason,
        "ontology_id": provenance.ontology_id,
        "ontology_version": provenance.ontology_version,
        "extractor_id": provenance.extractor_id,
        "extractor_version": provenance.extractor_version,
        "acl_ref": candidate.acl_ref,
    }

    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_source_key(source_id: str) -> str:
    """ASCII form of `source_id` whose ordering matches Python `str` ordering.

    Neo4j does not order non-BMP strings consistently with Python's
    code-point order, which made cursor pages skip candidates. UTF-8 byte
    order equals code-point order, so its hex form sorts identically
    everywhere.
    """
    return source_id.encode("utf-8").hex()


def candidate_sort_key(candidate: RejectedCandidate) -> tuple[str, str]:
    """Stable cross-backend ordering for candidate inspection."""
    return (
        candidate.provenance.source_id,
        candidate_storage_id(candidate),
    )


def encode_candidate_cursor(candidate: RejectedCandidate) -> str:
    """Encode the last displayed candidate's position."""
    import base64
    import json

    payload = json.dumps(
        candidate_sort_key(candidate),
        separators=(",", ":"),
    ).encode("utf-8")

    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


class InvalidCandidateCursorError(ValueError):
    """Invalid pagination cursor; safe to report without input details."""


def decode_candidate_cursor(cursor: str) -> tuple[str, str]:
    """Validate and decode a pagination position."""
    import base64
    import binascii
    import json
    import re

    try:
        if not cursor or len(cursor) > 8192:
            raise ValueError("invalid cursor")

        if re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ValueError("invalid cursor")

        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded = json.loads(raw)

        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not all(isinstance(value, str) for value in decoded)
            or not decoded[0]
            or re.fullmatch(r"[0-9a-f]{64}", decoded[1]) is None
        ):
            raise ValueError("invalid cursor")

        return decoded[0], decoded[1]

    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise InvalidCandidateCursorError("invalid candidate cursor") from None


@runtime_checkable
class CandidateStore(Protocol):
    """Separate storage for rejected, untrusted extraction candidates.

    Candidates must never be inserted into the trusted graph or exposed
    through GraphStore search, traversal, scan, or export methods.

    User-facing callers must additionally enforce ontology_reviewer
    authorization and emit the required audit record.
    """

    async def add_candidates(
        self,
        candidates: list[RejectedCandidate],
    ) -> None:
        """Persist candidates that have already been ACL-stamped by ingestion."""
        ...

    async def list_candidate_records(
        self,
        *,
        allowed_acls: list[str],
        include_public: bool = False,
        limit: int = 100,
        after: str | None = None,
    ) -> list[StoredCandidate]:
        """Return ACL-filtered candidates with their actual stored IDs."""
        ...

    async def list_candidates(
        self,
        *,
        allowed_acls: list[str],
        include_public: bool = False,
        limit: int = 100,
        after: str | None = None,
    ) -> list[RejectedCandidate]:
        """Return only candidates whose ACL the caller may VIEW.

        The caller's reviewer role must be verified before calling this.
        Public candidates (acl_ref=None) require an explicit representation
        of public VIEW permission; they must not bypass reviewer checks.
        """
        ...


class GraphStore(Protocol):
    """Storage contract.

    Writes are always additive (`add_fact`) or supersession-based
    (`close_fact`). No in-place mutation.
    """

    async def add_fact(self, fact: Fact) -> None: ...

    async def upsert_entity(self, entity: Entity, *, acl_ref: str | None = None) -> None:
        """Idempotent entity write with cross-ACL protection.

        Stores ``type``, ``canonical_name``, ``aliases``,
        ``properties``, a provenance pointer, and an optional
        ``acl_ref`` on the entity node. MUST be MERGE-based:
        re-ingesting the same entity never duplicates nodes.
        ``IngestPipeline.run`` calls this before ``add_fact`` so
        every ``Fact.subject_id`` / ``Fact.object_id`` has a
        backing typed node by the time the fact lands.

        **ACL semantics (#48):**

        =================  =================  =================================
        Stored ``acl_ref`` New ``acl_ref``    Action
        =================  =================  =================================
        ``None``           ``None``           Last-write-wins on attrs
        ``None``           ``X``              Upgrade: stamp X + update attrs
        ``X``              ``X``              Last-write-wins on attrs
        ``X``              ``None``           Raise ``CrossAclUpsertError``
        ``X``              ``Y`` ≠ ``X``      Raise ``CrossAclUpsertError``
        =================  =================  =================================

        Rationale: pre-#48 the ACL was first-write-wins but attrs
        were last-write-wins, which let attrs from one scope become
        readable under another scope's ACL (Aleksandra's #48 repro).
        The new contract keeps the ACL stable and rejects cross-scope
        attr overwrites atomically — no partial writes. Operators
        hitting ``CrossAclUpsertError`` have a resolver bug or need
        a dedicated admin path for multi-tenant merges.

        Entity properties are not bitemporal facts and have no
        supersession policy. Callers that need change history use
        the resolver's ``MergeRecord`` audit trail, not this seam.

        **Read-side ACL contract (CLAUDE.md invariant):** backends
        that persist entity attrs (name, aliases, properties) MUST
        filter at read time on ``acl_ref`` the same way ``search`` /
        ``traverse`` / ``facts_for_entity`` already do for facts.
        Returning an entity's attrs to a caller who can't see any
        fact citing it would leak the entity's existence — the
        permission-aware-traversal invariant forbids that. With #48
        the stored ACL is now the trustworthy ground truth for the
        attrs: they can never drift apart.
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

    async def entity_acl(self, entity_id: str) -> str | None:
        """Return the stored ``acl_ref`` for ``entity_id``, or ``None``
        if the entity is not stored or is public.

        Read-only lookup used by ``IngestPipeline`` to pre-check for
        cross-ACL conflicts across every extracted entity in a doc
        BEFORE any write, so a conflict leaves the store completely
        untouched (whole-doc atomicity under ``SKIP_AND_LOG``). The
        pre-check is advisory only — the authoritative check lives
        inside ``upsert_entity`` to close any TOCTOU window.

        Returns ``None`` both for unknown entities and entities with
        no ``acl_ref`` (public). Callers that need to distinguish
        the two cases should use ``facts_for_entity`` or inspect the
        store directly; the pre-check doesn't need that distinction
        because an unknown entity cannot have a conflicting ACL.
        """
        ...

    async def entities_for_source(
        self,
        source_id: str,
        *,
        acl_ref: str | None = None,
    ) -> list[StoredEntity]:
        """Return entity endpoints previously cited by one source.

        This is an internal ingest lookup used to stabilize raw entity ids
        across re-ingests of the same source. Membership is derived from fact
        provenance (`Fact.provenance.source_id`), not from the entity node's
        last-write-wins provenance pointer.

        Historical/closed facts count: an entity previously used by this
        source remains part of its identity history. Matching is strict
        same-ACL: public facts match only public facts, and an ACL-scoped
        ingest sees only facts carrying that exact acl_ref.

        This lookup is intentionally for the write path, not user-facing
        graph reads. IngestPipeline performs its normal cross-ACL entity
        pre-check immediately after reconciliation.
        """
        ...

    async def scan(
        self,
        *,
        as_of: datetime | None = None,
        include_closed: bool = False,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
        include_all_acls: bool = False,
    ) -> GraphScan:
        """Return a backend-neutral graph snapshot for export.

        By default only currently-active facts are returned. ``as_of``
        returns the graph visible at a historical point in time: facts
        valid at ``as_of`` and merge records with ``resolved_at <= as_of``.
        ``as_of`` must be timezone-aware (see ``ontos.runtime.temporal``).
        ``include_closed=True`` returns the full audit history and cannot
        be combined with ``as_of``.

        Raw entity ids and fact endpoints are preserved exactly as stored;
        resolution never rewrites them to canonical ids. Resolver decisions
        are returned separately in ``GraphScan.merges``.

        ACL filtering happens at the store boundary. Facts, entity
        attributes, and merge records that are not visible to the caller
        must not appear in the returned snapshot.

        ``include_all_acls=True`` is the explicit privileged audit-export
        escape hatch. Callers MUST authorize the dedicated audit-exporter
        role before setting it; ordinary user-facing reads must leave it
        False.
        """
        ...

    async def record_merge(self, record: MergeRecord) -> None:
        """Idempotent merge-record write.

        Persists one logical merge decision — a canonical entity id
        and the list of entity ids merged into it — so audit tools
        and #32's exporter can walk the trail across process
        restarts. Mirrors ``upsert_entity``'s MERGE semantics.

        **Idempotency key: ``(canonical_id, merged_id, resolver_id)``.**
        Re-recording the same merge updates ``resolved_at`` to the
        latest assertion but leaves one record / one edge per logical
        merge. A resolver asserting "A and B are the same" across N
        ingest runs produces one record, not N.

        **Self-loops stripped at write time.** ``ExactMatchResolver``
        includes ``canonical_id`` in ``merged_ids`` by convention
        (``rules.py``). Backends normalize the stored record so
        ``merged_ids`` contains only non-canonical ids — this is the
        shape ``merges_for_entity`` returns. Callers that need to know
        "every id in the group including canonical" use
        ``canonical_id + merged_ids``. A record whose ``merged_ids``
        contains only the canonical (no real consolidations) is
        skipped entirely — the audit edge only shows real consolidations.

        **Read-side ACL contract (CLAUDE.md invariant):** this seam
        does NOT itself carry an ACL. Readers (``merges_for_entity``,
        the exporter) filter the referenced entities via the
        entity-level ``acl_ref`` the same way ``facts_for_entity``
        already filters its facts. A merge record naming a restricted
        entity's id does not leak as long as the exporter checks both
        endpoint entities' ``acl_ref`` before emitting the edge.
        """
        ...

    async def merges_for_entity(
        self,
        entity_id: str,
        *,
        allowed_acls: list[str] | None = None,
    ) -> list[MergeRecord]:
        """Return every merge record where ``entity_id`` appears either
        as ``canonical_id`` OR in ``merged_ids``.

        Mirrors ``facts_for_entity`` — bidirectional audit lookup.
        One logical merge decision is returned once even if it carries
        multiple ``merged_ids`` (deduped on
        ``(canonical_id, resolver_id, resolved_at)``).

        **ACL pre-filter.** When ``allowed_acls`` is passed, every
        record whose canonical or any merged entity has an ``acl_ref``
        the caller cannot see is dropped at the store boundary. The
        default ``None`` means "no filter" (back-compat with pre-#47
        callers), but any caller that returns merge records up a user-
        facing path MUST pass ``allowed_acls`` or the authz-leak
        contract described on ``record_merge`` is violated: a merged
        id's existence would be visible even when the citing entity
        is not. CLAUDE.md "pre-filter when possible" applies here —
        the store sees the endpoint entities' ``acl_ref`` and must
        enforce the invariant on behalf of callers that forget.
        """
        ...

    async def close(self) -> None: ...


class StorageError(RuntimeError):
    """Raised when a store cannot load, persist, or recover state.

    Used by backends that persist to a local file (e.g. the dev-only
    pickle format of ``NetworkxStore``) to fail loud on magic-header
    mismatch, corrupt payloads, or version drift. Fail-loud is the
    right posture here: silent reset of persisted facts would quietly
    violate the provenance invariant.
    """


class CrossAclUpsertError(StorageError):
    """Raised when ``upsert_entity`` would write entity attributes
    under a different ACL than the one currently stamped on the
    entity node.

    Catches the compliance leak flagged in #48: an entity first
    stamped by a restricted doc, then re-upserted from a different
    (or public) scope, would otherwise end up with the original ACL
    but new attrs — a Finance caller reading HR-sourced attrs under
    the Finance stamp. Attrs are NEVER touched when this fires; the
    write is rejected atomically (and in the Neo4j backend, the
    transaction is rolled back).

    Operators who hit this have either (a) a resolver bug collapsing
    entities that shouldn't be collapsed, or (b) a legitimate
    multi-tenant merge that needs a dedicated admin path, not
    routine ingest. ``IngestPipeline.run`` routes this through
    ``ErrorPolicy`` the same way it routes ``ExtractionError``.
    """

    def __init__(
        self,
        *,
        entity_id: str,
        stored_acl: str | None,
        attempted_acl: str | None,
    ) -> None:
        self.entity_id = entity_id
        self.stored_acl = stored_acl
        self.attempted_acl = attempted_acl
        super().__init__(
            f"cross-ACL upsert rejected for entity_id={entity_id!r}: "
            f"stored acl_ref={stored_acl!r} but attempted write "
            f"carries acl_ref={attempted_acl!r}. Attrs were not "
            "written — fix the resolver that collapsed these "
            "entities or route the cross-scope merge through an "
            "admin path."
        )
