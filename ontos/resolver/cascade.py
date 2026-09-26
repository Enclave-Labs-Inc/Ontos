"""CascadeResolver — chain resolvers in order, forward unresolved to the next tier.

The Round-2 research consensus: cheap deterministic rules first, ML
statistical tier second, LLM only for the ambiguous long tail. The
cascade is just an ordered list; each tier's `unresolved` output
becomes the next tier's input. All tiers' merges accumulate into the
final `ResolutionResult` — audit sees every decision at every tier.

M4.a ships the Rules tier only (`ExactMatchResolver`). Adding an ML
tier (Splink / Zingg-shaped statistical matcher) or an LLM tier
(LLMBackend-driven disambiguation) is a follow-up — the CascadeResolver
accepts them via the same Protocol without any code change here.
"""

from __future__ import annotations

from ontos.resolver.base import MergeRecord, ResolutionResult, Resolver
from ontos.runtime.models import Entity


class CascadeResolver:
    """Runs an ordered list of Resolvers; each tier gets the previous tier's unresolved."""

    def __init__(self, tiers: list[Resolver]) -> None:
        if not tiers:
            raise ValueError("CascadeResolver requires at least one tier")
        self._tiers = tiers

    @property
    def id(self) -> str:
        return "ontos.resolver.cascade[" + ",".join(t.id for t in self._tiers) + "]"

    @property
    def version(self) -> str:
        return "+".join(t.version for t in self._tiers)

    async def resolve(self, candidates: list[Entity]) -> ResolutionResult:
        collected_entities: list[Entity] = []
        collected_merges: list[MergeRecord] = []
        remaining: list[Entity] = list(candidates)

        for tier in self._tiers:
            if not remaining:
                break
            result = await tier.resolve(remaining)
            collected_entities.extend(result.entities)
            collected_merges.extend(result.merges)
            remaining = list(result.unresolved)

        # Anything still unresolved at the end passes through as-is; the
        # caller decides whether to treat "no match" as a soft or hard
        # signal. The cascade doesn't drop entities.
        return ResolutionResult(
            entities=[*collected_entities, *remaining],
            merges=collected_merges,
            unresolved=[],
        )
