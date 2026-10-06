"""NetworkX store for dev / local iteration, with optional file persistence.

DEV ONLY. Not for regulated deploy — single-process, pickle-based
on-disk format, no crash safety beyond an atomic save, no concurrency
guarantees. Neo4j is the production backend.

Persistence format
------------------

When constructed with ``path=``, the store transparently loads from
and saves to that file. The on-disk payload is:

    b"ONTOS-NX-STORE-V2\\n" + pickle.dumps({"graph": ..., "facts": ...,
                                             "merges": ...})

Legacy format compat: files written under ``ONTOS-NX-STORE-V1``
(no ``merges`` key) load cleanly with an empty merges dict and get
rewritten under V2 on the next flush. One structlog info line
("networkx-store-migrated-from-legacy-format") fires per load.

``pickle`` is a Python-version-specific binary format, NOT a wire
format. The file is intended to live under the operator's own home
directory and be read + written only by the same user's `ontos` CLI.
Future format bumps (V3 and beyond) will accept V2 as legacy and
``StorageError`` on genuinely-unrecognised headers.

Save is atomic: ``write_bytes`` to ``<path>.tmp`` then ``replace``.
A crash mid-save leaves the previous valid file intact.
"""

from __future__ import annotations

import contextlib
import pickle
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import networkx as nx
import structlog

from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Entity, Fact
from ontos.storage.base import StorageError

log = structlog.get_logger(__name__)


def _fact_alive_at(fact: Fact, ts: datetime | None) -> bool:
    if ts is None:
        return fact.t_invalid is None
    if fact.t_valid > ts:
        return False
    return fact.t_invalid is None or fact.t_invalid > ts


class NetworkxStore:
    """MultiDiGraph-backed store. Facts are edges; entities are nodes.

    Each edge key is the fact UUID so multiple facts between the same pair
    of entities can coexist.

    Pass ``path`` to persist state across process invocations (fixes #29).
    Omit ``path`` for an ephemeral in-memory store (what tests use).
    """

    # V2 adds the `_merges` payload key for persisted MergeRecords (#46).
    # V1 files load with an empty `_merges` default and get upgraded on
    # next flush — operators see a one-line structlog info and no action
    # is required.
    _MAGIC_HEADER = b"ONTOS-NX-STORE-V2\n"
    _LEGACY_MAGIC_HEADERS: tuple[bytes, ...] = (b"ONTOS-NX-STORE-V1\n",)

    def __init__(self, path: Path | None = None) -> None:
        self._graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        self._facts: dict[UUID, Fact] = {}
        # Keyed on (canonical_id, merged_id, resolver_id) — idempotent on
        # re-record; last-write-wins on resolved_at. See #46 and the
        # GraphStore.record_merge Protocol docstring for semantics.
        self._merges: dict[tuple[str, str, str], MergeRecord] = {}
        self._path: Path | None = path
        # Set by any state-mutating method; checked by `flush()` so that
        # read-only callers (e.g. `ontos query`) never rewrite the file —
        # that would clobber a concurrent writer's newer snapshot.
        self._dirty: bool = False
        if path is not None and path.exists() and path.stat().st_size > 0:
            self._load_from(path)

    def _load_from(self, path: Path) -> None:
        """Load state from path. Fail loud on magic mismatch or corrupt payload.

        Accepts the current magic header and any header in
        ``_LEGACY_MAGIC_HEADERS``; legacy loads log a one-line
        structlog info naming the migration so operators see what
        happened. Missing payload keys default to the empty collection
        for forward-compat with older on-disk formats.
        """
        raw = path.read_bytes()
        if raw.startswith(self._MAGIC_HEADER):
            payload_bytes = raw[len(self._MAGIC_HEADER) :]
        elif legacy := next(
            (h for h in self._LEGACY_MAGIC_HEADERS if raw.startswith(h)),
            None,
        ):
            log.info(
                "networkx-store-migrated-from-legacy-format",
                path=str(path),
                from_version=legacy.decode().strip(),
                to_version=self._MAGIC_HEADER.decode().strip(),
            )
            payload_bytes = raw[len(legacy) :]
        else:
            raise StorageError(
                f"{path} is not an Ontos NetworkxStore file "
                f"(missing {self._MAGIC_HEADER!r} or any legacy header "
                f"in {self._LEGACY_MAGIC_HEADERS!r}). Delete it or "
                "point at a different --storage-path."
            )

        try:
            payload = pickle.loads(payload_bytes)
        except (
            # Broadened to cover realistic version-drift cases where a
            # pickled module / class moved or was renamed between releases;
            # without ImportError / IndexError / TypeError here the raw
            # exception escapes and the "fail loud with StorageError"
            # contract is violated. Reported on PR #41.
            pickle.UnpicklingError,
            EOFError,
            AttributeError,
            ImportError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise StorageError(
                f"{path} has valid header but corrupt payload: {exc!r}. "
                "Delete it to start fresh, or restore from a backup. "
                "(Likely an Ontos version drift — pickled class moved or renamed.)"
            ) from exc
        try:
            self._graph = payload["graph"]
            self._facts = payload["facts"]
            # V1 payloads lack "merges"; default to empty (the lazy
            # migration path — on next flush the file gets rewritten
            # under the current magic header with the new key).
            self._merges = payload.get("merges", {})
        except (KeyError, TypeError) as exc:
            raise StorageError(
                f"{path} payload shape is not recognised: {exc!r}. "
                "File may be from a different Ontos version."
            ) from exc

    def flush(self, *, force: bool = False) -> None:
        """Persist state to self._path right now.

        No-op if path is None, or if nothing has mutated since the last
        load/flush (``self._dirty is False``). The dirty-gate stops
        read-only callers (`ontos query`) from rewriting the file —
        without it, a stale-snapshot query would clobber a concurrent
        ingest's newer facts on exit. Pass ``force=True`` to override
        (useful for tests and explicit "snapshot now" semantics).
        """
        if self._path is None:
            return
        if not self._dirty and not force:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(
            self._MAGIC_HEADER
            + pickle.dumps({"graph": self._graph, "facts": self._facts, "merges": self._merges})
        )
        # Dev-store file: single-user, same-process-writer. Lock down
        # mode so a shared machine doesn't let other users plant a
        # malicious pickle at the predictable default path (pickle.loads
        # executes arbitrary code).
        # chmod not supported on this filesystem (rare, e.g. some mounts
        # on Windows); carry on.
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        tmp.replace(self._path)  # atomic swap
        self._dirty = False

    @property
    def path(self) -> Path | None:
        """The persistence path for this store, or None for in-memory."""
        return self._path

    async def add_fact(self, fact: Fact) -> None:
        self._graph.add_node(fact.subject_id)
        self._graph.add_node(fact.object_id)
        self._graph.add_edge(fact.subject_id, fact.object_id, key=fact.id, fact=fact)
        self._facts[fact.id] = fact
        self._dirty = True

    async def close_fact(
        self, fact_id: UUID, t_invalid: datetime, superseded_by: UUID | None = None
    ) -> None:
        current = self._facts.get(fact_id)
        if current is None:
            return
        replacement = current.model_copy(
            update={"t_invalid": t_invalid, "superseded_by": superseded_by}
        )
        self._facts[fact_id] = replacement
        # The stubs type edge keys as str, but MultiDiGraph accepts any hashable
        # (see networkx MultiDiGraph.add_edge docs); we key on the UUID at runtime.
        self._graph[current.subject_id][current.object_id][fact_id]["fact"] = replacement  # type: ignore[index]
        self._dirty = True

    async def upsert_entity(self, entity: Entity, *, acl_ref: str | None = None) -> None:
        # networkx.add_node(id, **attrs) merges attrs on repeated calls
        # (idempotent MERGE semantics). The persistence pickle serializes
        # the raw MultiDiGraph which carries node attrs natively, so a
        # second-process read sees these values without any format bump.
        existing_attrs = self._graph.nodes.get(entity.id, {})
        existing_type = existing_attrs.get("type")
        if existing_type is not None and existing_type != entity.type:
            # Last-write-wins is the stored contract, but surface the
            # type drift so operators can audit a resolver bug or an
            # upstream schema change rather than seeing silent overwrite.
            log.warning(
                "entity-type-overwrite",
                entity_id=entity.id,
                previous_type=existing_type,
                new_type=entity.type,
                canonical_name=entity.canonical_name,
            )
        # ACL is first-write-wins: a restricted doc's stamp persists
        # even when a later public ingest re-upserts the same entity.
        # Avoids a silent downgrade that would expose a previously-
        # restricted entity's name via #32's exporter.
        effective_acl = existing_attrs.get("acl_ref") or acl_ref
        self._graph.add_node(
            entity.id,
            type=entity.type,
            canonical_name=entity.canonical_name,
            aliases=list(entity.aliases),
            properties=dict(entity.properties),
            provenance_source_id=entity.provenance.source_id,
            provenance_extractor_id=entity.provenance.extractor_id,
            acl_ref=effective_acl,
        )
        self._dirty = True

    async def get_fact(self, fact_id: UUID) -> Fact | None:
        return self._facts.get(fact_id)

    async def search(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        k: int = 10,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]:
        # M0: substring match over predicate + object_id, ranked by naive relevance.
        # M3 replaces this with vector + BM25 hybrid; ACL is enforced pre-return.
        needle = query.lower()
        matches: list[tuple[float, Fact]] = []
        allowed_set = set(allowed_acls) if allowed_acls is not None else None
        for fact in self._facts.values():
            if not _fact_alive_at(fact, as_of):
                continue
            if not _acl_allows(fact, acl_subject, allowed_set):
                continue
            hay = f"{fact.subject_id} {fact.predicate} {fact.object_id}".lower()
            if needle in hay:
                score = 1.0 if needle == hay else 0.5 + (len(needle) / max(len(hay), 1)) * 0.5
                matches.append((score, fact))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return [fact for _, fact in matches[:k]]

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
    ) -> Iterable[Fact]:
        if start not in self._graph:
            return []
        collected: list[Fact] = []
        frontier: set[str] = {start}
        seen: set[str] = {start}
        allowed_set = set(allowed_acls) if allowed_acls is not None else None

        for _ in range(depth):
            next_frontier: set[str] = set()

            for node in frontier:
                if direction in ("out", "both"):
                    for _, target, _, data in self._graph.out_edges(node, keys=True, data=True):
                        fact: Fact = data["fact"]
                        if not _fact_alive_at(fact, as_of):
                            continue
                        if relation and fact.predicate != relation:
                            continue
                        if not _acl_allows(fact, acl_subject, allowed_set):
                            continue

                        collected.append(fact)

                        if target not in seen:
                            seen.add(target)
                            next_frontier.add(target)

                if direction in ("in", "both"):
                    for source, _, _, data in self._graph.in_edges(node, keys=True, data=True):
                        fact = data["fact"]
                        if not _fact_alive_at(fact, as_of):
                            continue
                        if relation and fact.predicate != relation:
                            continue
                        if not _acl_allows(fact, acl_subject, allowed_set):
                            continue

                        collected.append(fact)

                        if source not in seen:
                            seen.add(source)
                            next_frontier.add(source)

            frontier = next_frontier
            if not frontier:
                break

        return collected

    async def facts_for_entity(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> list[Fact]:
        if entity_id not in self._graph:
            return []
        out: list[Fact] = []
        allowed_set = set(allowed_acls) if allowed_acls is not None else None
        for _, _, _, data in self._graph.out_edges(entity_id, keys=True, data=True):
            fact: Fact = data["fact"]
            if _fact_alive_at(fact, as_of) and _acl_allows(fact, acl_subject, allowed_set):
                out.append(fact)
        for _, _, _, data in self._graph.in_edges(entity_id, keys=True, data=True):
            fact = data["fact"]
            if _fact_alive_at(fact, as_of) and _acl_allows(fact, acl_subject, allowed_set):
                out.append(fact)
        return out

    async def record_merge(self, record: MergeRecord) -> None:
        # Fan out one logical MergeRecord across (canonical, merged_id,
        # resolver_id) keys so merges_for_entity can find the record
        # from any endpoint.
        #
        # Normalize at write time: strip the canonical from merged_ids
        # before storing. ExactMatchResolver's convention includes the
        # winner's own id in merged_ids; the audit edge only shows real
        # consolidations, and reads must match Neo4j (which also strips
        # the self-loop). The normalized record is what merges_for_entity
        # returns — Protocol docstring documents the contract.
        normalized_merged = [m for m in record.merged_ids if m != record.canonical_id]
        if not normalized_merged:
            # No real consolidation (resolver only emitted the canonical
            # self-reference); nothing to record.
            return
        normalized = record.model_copy(update={"merged_ids": normalized_merged})
        for merged_id in normalized_merged:
            key = (record.canonical_id, merged_id, record.resolver_id)
            # Idempotent: re-recording the same (canonical, merged,
            # resolver) tuple is last-write-wins on resolved_at + reason.
            self._merges[key] = normalized
        self._dirty = True

    async def merges_for_entity(
        self,
        entity_id: str,
        *,
        allowed_acls: list[str] | None = None,
    ) -> list[MergeRecord]:
        # Dedupe on (canonical_id, resolver_id, resolved_at): one logical
        # MergeRecord fans out across multiple keys (one per merged_id)
        # but should be returned once from the audit view.
        #
        # PR #47 review: optional allowed_acls pre-filter drops records
        # whose canonical or any merged endpoint has an acl_ref the
        # caller cannot see. Readers that forget to filter would leak
        # the existence of a restricted entity's merge group — CLAUDE.md
        # forbids that.
        allowed_set = set(allowed_acls) if allowed_acls is not None else None
        seen: set[tuple[str, str, str]] = set()
        hits: list[MergeRecord] = []
        for (canonical, merged, _resolver), record in self._merges.items():
            if canonical != entity_id and merged != entity_id:
                continue
            dedupe_key = (
                record.canonical_id,
                record.resolver_id,
                record.resolved_at.isoformat(),
            )
            if dedupe_key in seen:
                continue
            if allowed_set is not None and not self._merge_record_visible(record, allowed_set):
                seen.add(dedupe_key)  # dedupe even skipped records to avoid re-check
                continue
            seen.add(dedupe_key)
            hits.append(record)
        return hits

    def _merge_record_visible(self, record: MergeRecord, allowed_set: set[str]) -> bool:
        """Return True iff every endpoint entity is visible under allowed_set.

        An endpoint is visible when its node's ``acl_ref`` is None (public)
        or appears in ``allowed_set``. If the node doesn't exist in the
        graph (e.g. a merged id that was never upserted), we treat it as
        public — the pipeline guarantees upserts come before fact writes
        in #38, but older stores may have gaps.
        """

        def _endpoint_visible(eid: str) -> bool:
            attrs = self._graph.nodes.get(eid, {})
            acl = attrs.get("acl_ref")
            return acl is None or acl in allowed_set

        if not _endpoint_visible(record.canonical_id):
            return False
        return all(_endpoint_visible(mid) for mid in record.merged_ids)

    async def close(self) -> None:
        self.flush()
        # Keep the clear so existing in-memory callers see close() as a
        # reset. Persisted callers already have their state on disk; the
        # next NetworkxStore(path=same) rebuilds from the file.
        self._graph.clear()
        self._facts.clear()
        self._merges.clear()


def _acl_allows(
    fact: Fact,
    acl_subject: str | None,
    allowed_acls: set[str] | None,
) -> bool:
    """Deny-by-default when a fact declares an acl_ref and no caller identity is supplied.

    - `fact.acl_ref is None`: public. Always visible.
    - `allowed_acls is not None`: M2 authz-resolved path. Fact visible iff its
      `acl_ref` is in the resolved allow-list.
    - `allowed_acls is None` and `acl_subject is not None`: M1 shim (dev-only).
      Fact visible iff `fact.acl_ref == acl_subject`.
    - Both `None` and `acl_ref` set: DENY.
    """
    if fact.acl_ref is None:
        return True
    if allowed_acls is not None:
        return fact.acl_ref in allowed_acls
    if acl_subject is not None:
        return fact.acl_ref == acl_subject
    return False
