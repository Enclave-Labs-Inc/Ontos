"""NetworkX store for dev / local iteration, with optional file persistence.

DEV ONLY. Not for regulated deploy — single-process, pickle-based
on-disk format, no crash safety beyond an atomic save, no concurrency
guarantees. Neo4j is the production backend.

Persistence format
------------------

When constructed with ``path=``, the store transparently loads from
and saves to that file. The on-disk payload is:

    b"ONTOS-NX-STORE-V1\\n" + pickle.dumps({"graph": ..., "facts": ...})

``pickle`` is a Python-version-specific binary format, NOT a wire
format. The file is intended to live under the operator's own home
directory and be read + written only by the same user's `ontos` CLI.
A format bump (V2 and beyond) will fail loud on load with
``StorageError`` rather than silently reset state.

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

from ontos.runtime.models import Fact
from ontos.storage.base import StorageError


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

    _MAGIC_HEADER = b"ONTOS-NX-STORE-V1\n"

    def __init__(self, path: Path | None = None) -> None:
        self._graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        self._facts: dict[UUID, Fact] = {}
        self._path: Path | None = path
        # Set by any state-mutating method; checked by `flush()` so that
        # read-only callers (e.g. `ontos query`) never rewrite the file —
        # that would clobber a concurrent writer's newer snapshot.
        self._dirty: bool = False
        if path is not None and path.exists() and path.stat().st_size > 0:
            self._load_from(path)

    def _load_from(self, path: Path) -> None:
        """Load state from path. Fail loud on magic mismatch or corrupt payload."""
        raw = path.read_bytes()
        if not raw.startswith(self._MAGIC_HEADER):
            raise StorageError(
                f"{path} is not an Ontos NetworkxStore v1 file "
                f"(missing {self._MAGIC_HEADER!r} header). Delete it or "
                "point at a different --storage-path."
            )
        try:
            payload = pickle.loads(raw[len(self._MAGIC_HEADER) :])
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
            self._MAGIC_HEADER + pickle.dumps({"graph": self._graph, "facts": self._facts})
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

    async def close(self) -> None:
        self.flush()
        # Keep the clear so existing in-memory callers see close() as a
        # reset. Persisted callers already have their state on disk; the
        # next NetworkxStore(path=same) rebuilds from the file.
        self._graph.clear()
        self._facts.clear()


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
