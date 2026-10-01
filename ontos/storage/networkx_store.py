"""In-memory NetworkX store for M0 dev/iteration.

DEV ONLY. Not for regulated deploy — no persistence, no crash safety,
no concurrency guarantees. M1 replaces this with Neo4j.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal
from uuid import UUID

import networkx as nx

from ontos.runtime.models import Fact


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
    """

    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        self._facts: dict[UUID, Fact] = {}

    async def add_fact(self, fact: Fact) -> None:
        self._graph.add_node(fact.subject_id)
        self._graph.add_node(fact.object_id)
        self._graph.add_edge(fact.subject_id, fact.object_id, key=fact.id, fact=fact)
        self._facts[fact.id] = fact

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
