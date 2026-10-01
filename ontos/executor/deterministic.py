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

import re
from datetime import datetime
from typing import Any, Literal

import networkx as nx

from ontos.executor.base import ExecutionHit, ExecutionResult
from ontos.planner import Plan, SeedByEntity, SeedByKeyword
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore

# Matches a trailing " (Word)" or " (Word_word2)" annotation — the
# exact shape the LLM planner tends to append when it thinks it's
# being helpful (e.g. "Alice Johnson (Person)"). We only strip when
# the parenthesized content is a single identifier-shaped token to
# avoid clobbering legitimate names like "Meridian (US) Inc.".
_TYPE_SUFFIX_RE = re.compile(r"\s+\(([A-Za-z][A-Za-z0-9_-]*)\)\s*$")

# Search window for the keyword-fallback ambiguity check. Deliberately
# large so a popular entity (many facts about "Acme Corp") can't crowd
# a longer-named sibling ("Acme Corp Europe") out of the window and
# cause a silent wrong-entity resolution. If the window still comes
# back saturated, we add a "possibly-incomplete" warning rather than
# refuse — refusing on saturation would break resolution for any
# entity with ≥50 facts, which is routine in production.
_SEED_FALLBACK_SEARCH_K: int = 50

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

        seed_entities = await self._seed(plan, store, as_of, acl_subject, allowed_acls, warnings)
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
        warnings: list[str],
    ) -> list[str]:
        if isinstance(plan.seed, SeedByEntity):
            resolved = await self._resolve_seed_entity(
                plan.seed.entity_id,
                store,
                as_of,
                acl_subject,
                allowed_acls,
                warnings,
            )
            return [resolved] if resolved is not None else []
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

    async def _resolve_seed_entity(
        self,
        entity_id: str,
        store: GraphStore,
        as_of: datetime | None,
        acl_subject: str | None,
        allowed_acls: list[str] | None,
        warnings: list[str],
    ) -> str | None:
        """Resolve a planner-supplied entity_id to one that lives in the graph.

        The LLM planner can drift — appending "(Person)" or other type
        annotations to what should be a bare entity id, wrapping in
        quotes, adding whitespace. Relying on exact-string identity
        between planner output and stored id turns those drifts into
        silent "no facts" outcomes (github issue #16).

        Fallback chain, each step emits a warning on match so the
        operator can see resolution happened:
          1. Exact match — id is already correct, no warning.
          2. Strip trailing " (Type)" annotation and retry.
          3. Search the store for the cleaned-up name. Resolve ONLY
             when there is a single unambiguous candidate; on
             multiple matches, emit an ambiguity warning naming the
             candidates and return None instead of guessing.
        Returns None if all three miss OR if the search fallback is
        ambiguous — silent wrong-entity resolution is worse than an
        explicit failure that surfaces the ambiguity.
        """
        # 1. Exact match — id is already what the store uses.
        if await store.facts_for_entity(
            entity_id,
            as_of=as_of,
            acl_subject=acl_subject,
            allowed_acls=allowed_acls,
        ):
            return entity_id

        # 2. Strip trailing " (Type)" annotation.
        if _TYPE_SUFFIX_RE.search(entity_id):
            stripped = _TYPE_SUFFIX_RE.sub("", entity_id).strip()
            if stripped and await store.facts_for_entity(
                stripped,
                as_of=as_of,
                acl_subject=acl_subject,
                allowed_acls=allowed_acls,
            ):
                warnings.append(
                    f"seed entity {entity_id!r} resolved to {stripped!r} via type-suffix strip"
                )
                return stripped

        # 3. Keyword-search fallback on the cleaned-up name.
        # Search does substring match; can still succeed after
        # step 1's exact-id lookup missed. Collect every distinct
        # substring-matching endpoint from the hits — if more than one,
        # refuse to guess (silent wrong-entity resolution is a worse
        # failure mode than an explicit ambiguity warning).
        #
        # Window size is _SEED_FALLBACK_SEARCH_K (50); if the window
        # comes back saturated (len(hits) == k), a longer-named sibling
        # could be beyond it, so we emit an extra "possibly-incomplete"
        # warning on top of whatever we resolve. Refusing to resolve on
        # saturation would break every entity with ≥50 facts.
        needle = _TYPE_SUFFIX_RE.sub("", entity_id).strip() or entity_id
        hits = await store.search(
            needle,
            as_of=as_of,
            k=_SEED_FALLBACK_SEARCH_K,
            acl_subject=acl_subject,
            allowed_acls=allowed_acls,
        )
        candidates: list[str] = []
        seen_candidates: set[str] = set()
        needle_lower = needle.lower()
        for f in hits:
            for endpoint in (f.subject_id, f.object_id):
                if needle_lower in endpoint.lower() and endpoint not in seen_candidates:
                    candidates.append(endpoint)
                    seen_candidates.add(endpoint)

        window_saturated = len(hits) >= _SEED_FALLBACK_SEARCH_K

        if len(candidates) == 1:
            warnings.append(
                f"seed entity {entity_id!r} resolved to "
                f"{candidates[0]!r} via keyword search fallback"
            )
            if window_saturated:
                warnings.append(
                    f"seed entity {entity_id!r} search window was "
                    f"saturated (k={_SEED_FALLBACK_SEARCH_K}) — "
                    "additional substring-matching entities may exist "
                    "beyond the window; use SeedByKeyword or pass an "
                    "exact id if the resolved entity is wrong"
                )
            return candidates[0]
        if len(candidates) > 1:
            preview = ", ".join(repr(c) for c in candidates[:5])
            tail = "" if len(candidates) <= 5 else f", ... ({len(candidates)} total)"
            warnings.append(
                f"seed entity {entity_id!r} matches multiple graph "
                f"entities via keyword search ({preview}{tail}); "
                "refusing to guess — pass an exact id or use "
                "SeedByKeyword to enumerate candidates"
            )
            return None

        return None

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
                                direction=step.direction,
                                depth=step_depth,
                                as_of=as_of,
                                acl_subject=acl_subject,
                                allowed_acls=allowed_acls,
                            )
                        )
                        _absorb(
                            facts,
                            collected,
                            hops_by_fact,
                            current_hop + 1,
                            next_frontier,
                            step.direction,
                        )
                else:
                    facts = list(
                        await store.traverse(
                            start,
                            direction=step.direction,
                            depth=step_depth,
                            as_of=as_of,
                            acl_subject=acl_subject,
                            allowed_acls=allowed_acls,
                        )
                    )
                    _absorb(
                        facts,
                        collected,
                        hops_by_fact,
                        current_hop + 1,
                        next_frontier,
                        step.direction,
                    )
            current_hop += step_depth
            current_frontier = list(next_frontier)
            if not current_frontier:
                break
        return collected, hops_by_fact

    def _ppr(self, collected: dict[Any, Fact], seed_entities: list[str]) -> dict[str, float]:
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
    direction: Literal["out", "in", "both"],
) -> None:
    """Merge traversal facts and advance the frontier in traversal direction."""
    for f in facts:
        if f.id not in collected:
            collected[f.id] = f
            hops_by_fact[f.id] = hop_distance
        else:
            hops_by_fact[f.id] = min(hops_by_fact[f.id], hop_distance)

        if direction in ("out", "both"):
            next_frontier.add(f.object_id)
        if direction in ("in", "both"):
            next_frontier.add(f.subject_id)
