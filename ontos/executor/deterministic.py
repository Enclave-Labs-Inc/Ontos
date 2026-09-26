"""DeterministicExecutor — the M3 executor.

Pipeline per plan:

1. **Seed**: resolve `plan.seed` → initial entity ids via the store.
   - `SeedByEntity`: `{entity_id}` (a single-node seed).
   - `SeedByKeyword`: substring-search the store and take the top-k
     entity ids from the matching facts.
2. **Expand**: for each `TraversalStep`, expand the current frontier
   by calling `store.traverse(...)` with the step's relations, depth,
   and direction. Collect all facts touched.
3. **Rank via PPR**: build a NetworkX graph from the collected facts,
   run Personalized-PageRank with weight on the seed nodes only
   (HippoRAG-style single-step activation), then score each fact by
   the max(source PPR, target PPR).
4. **Path-flow scoring (PathRAG-style)**: for each fact, propagate a
   resource of 1.0 from the seed with per-hop decay α; drop facts
   whose accumulated flow falls below θ.
5. **Combine + rank**: `combined_score = ppr_score * path_score`, sort
   descending, truncate to `plan.limit`.

Nothing here calls an LLM. `same plan + same store = same ranking`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import networkx as nx

from ontos.executor.base import ExecutionHit, ExecutionResult
from ontos.planner import Plan, SeedByEntity, SeedByKeyword
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore

# HippoRAG uses damping 0.5; PathRAG uses α ∈ [0.6, 0.9] with θ = 0.05.
# We pick middle-of-the-road defaults that make small in-memory graphs
# behave sensibly; production tuning is a Scribe-era concern.
_PPR_DAMPING: float = 0.5
_PPR_MAX_ITER: int = 100
_PATH_DECAY: float = 0.8
_PATH_EARLY_STOP: float = 0.05
_EXECUTOR_MAX_DEPTH: int = 10


class DeterministicExecutor:
    """PPR + PathRAG-style executor."""

    id: str = "ontos.executor.deterministic-v1"

    async def execute(
        self,
        plan: Plan,
        store: GraphStore,
        *,
        acl_subject: str | None = None,
        allowed_acls: list[str] | None = None,
    ) -> ExecutionResult:
        as_of: datetime | None = plan.as_of
        warnings: list[str] = []

        seed_entities = await self._seed(plan, store, as_of, acl_subject, allowed_acls)
        if not seed_entities:
            warnings.append("plan.seed produced no entities — nothing to rank")
            return ExecutionResult(hits=[], warnings=warnings)

        collected, hops_by_fact = await self._expand(
            plan, seed_entities, store, as_of, acl_subject, allowed_acls
        )
        if not collected:
            warnings.append("expansion produced no facts")
            return ExecutionResult(hits=[], warnings=warnings)

        node_ppr = self._ppr(collected, seed_entities)
        path_scores = self._path_scores(collected, seed_entities, hops_by_fact)

        hits: list[ExecutionHit] = []
        for fact_id, fact in collected.items():
            source_ppr = node_ppr.get(fact.subject_id, 0.0)
            target_ppr = node_ppr.get(fact.object_id, 0.0)
            ppr = max(source_ppr, target_ppr)
            path = path_scores.get(fact_id, 0.0)
            if path < _PATH_EARLY_STOP:
                continue
            combined = ppr * path
            hits.append(
                ExecutionHit(
                    fact=fact,
                    combined_score=combined,
                    ppr_score=ppr,
                    path_score=path,
                    hops_from_seed=hops_by_fact.get(fact_id, 0),
                )
            )

        hits.sort(key=lambda h: h.combined_score, reverse=True)
        return ExecutionResult(hits=hits[: plan.limit], warnings=warnings)

    async def _seed(
        self,
        plan: Plan,
        store: GraphStore,
        as_of: datetime | None,
        acl_subject: str | None,
        allowed_acls: list[str] | None,
    ) -> list[str]:
        if isinstance(plan.seed, SeedByEntity):
            return [plan.seed.entity_id]
        if isinstance(plan.seed, SeedByKeyword):
            hits = await store.search(
                plan.seed.keyword,
                as_of=as_of,
                k=plan.seed.k,
                acl_subject=acl_subject,
                allowed_acls=allowed_acls,
            )
            ids: list[str] = []
            for f in hits:
                if f.subject_id not in ids:
                    ids.append(f.subject_id)
                if f.object_id not in ids:
                    ids.append(f.object_id)
            return ids[: plan.seed.k]
        raise TypeError(f"unknown seed variant: {plan.seed!r}")

    async def _expand(
        self,
        plan: Plan,
        seed_entities: list[str],
        store: GraphStore,
        as_of: datetime | None,
        acl_subject: str | None,
        allowed_acls: list[str] | None,
    ) -> tuple[dict[Any, Fact], dict[Any, int]]:
        """Return (facts by id, hop distance from nearest seed per fact)."""
        collected: dict[Any, Fact] = {}
        hops_by_fact: dict[Any, int] = {}
        current_frontier: list[str] = list(dict.fromkeys(seed_entities))
        current_hop = 0
        for step in plan.steps:
            step_depth = min(step.depth, _EXECUTOR_MAX_DEPTH)
            next_frontier: set[str] = set()
            for start in current_frontier:
                if step.relations:
                    for relation in step.relations:
                        facts = list(
                            await store.traverse(
                                start,
                                relation=relation,
                                depth=step_depth,
                                as_of=as_of,
                                acl_subject=acl_subject,
                                allowed_acls=allowed_acls,
                            )
                        )
                        _absorb(facts, collected, hops_by_fact, current_hop + 1, next_frontier)
                else:
                    facts = list(
                        await store.traverse(
                            start,
                            depth=step_depth,
                            as_of=as_of,
                            acl_subject=acl_subject,
                            allowed_acls=allowed_acls,
                        )
                    )
                    _absorb(facts, collected, hops_by_fact, current_hop + 1, next_frontier)
            current_hop += step_depth
            current_frontier = list(next_frontier)
            if not current_frontier:
                break
        return collected, hops_by_fact

    def _ppr(
        self, collected: dict[Any, Fact], seed_entities: list[str]
    ) -> dict[str, float]:
        """Personalized PageRank over the fetched subgraph."""
        g: nx.DiGraph[str] = nx.DiGraph()
        for fact in collected.values():
            weight = float(fact.provenance.confidence_score)
            g.add_edge(fact.subject_id, fact.object_id, weight=weight)
        seed_nodes = {s for s in seed_entities if s in g}
        if not seed_nodes:
            return {}
        personalization = {n: (1.0 if n in seed_nodes else 0.0) for n in g.nodes}
        try:
            return dict(
                nx.pagerank(
                    g,
                    alpha=_PPR_DAMPING,
                    personalization=personalization,
                    max_iter=_PPR_MAX_ITER,
                )
            )
        except nx.PowerIterationFailedConvergence:
            # PPR non-convergence on a tiny graph is a degenerate case;
            # fall back to uniform seed weight.
            return {n: (1.0 if n in seed_nodes else 0.0) for n in g.nodes}

    def _path_scores(
        self,
        collected: dict[Any, Fact],
        seed_entities: list[str],
        hops_by_fact: dict[Any, int],
    ) -> dict[Any, float]:
        """PathRAG-style resource propagation with per-hop decay.

        Score falls off as `α ** hops`, weighted by fact confidence.
        """
        seed_set = set(seed_entities)
        scores: dict[Any, float] = {}
        for fact_id, fact in collected.items():
            hops = hops_by_fact.get(fact_id, 0)
            confidence = float(fact.provenance.confidence_score)
            base = (_PATH_DECAY**hops) * confidence
            # Bonus if the fact directly touches a seed — reproduces
            # the PPR "concentrate around seed" behavior at path scale.
            if fact.subject_id in seed_set or fact.object_id in seed_set:
                base *= 1.25
            scores[fact_id] = min(base, 1.0)
        return scores


def _absorb(
    facts: list[Fact],
    collected: dict[Any, Fact],
    hops_by_fact: dict[Any, int],
    hop_distance: int,
    next_frontier: set[str],
) -> None:
    """Merge a batch of facts from one traversal call into the running state."""
    for f in facts:
        if f.id not in collected:
            collected[f.id] = f
            hops_by_fact[f.id] = hop_distance
        else:
            hops_by_fact[f.id] = min(hops_by_fact[f.id], hop_distance)
        next_frontier.add(f.subject_id)
        next_frontier.add(f.object_id)
