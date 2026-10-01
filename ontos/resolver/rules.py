"""ExactMatchResolver — the Rules tier of the resolution cascade.

Deterministic. Cheap. Handles the majority of matches in practice
(same canonical_name after normalization, same alias, same type).
Whatever falls through goes to the next tier (ML in a future M4.a
follow-up, LLM after that).

Normalization for M4.a v1: lowercase + strip. Enough for the starter
ontology and the dev corpus; enterprise deploys will layer richer
normalizers (unicode normalization, honorific stripping, common
address canonicalization) as they encounter them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from ontos.resolver.base import MergeRecord, ResolutionResult, Resolver
from ontos.runtime.models import Entity


def _canonical_key(entity: Entity) -> str:
    return f"{entity.type}::{entity.canonical_name.strip().casefold()}"


class ExactMatchResolver:
    """Merges candidates whose (type, normalized canonical_name) collide."""

    id: str = "ontos.resolver.exact-match"
    version: str = "0.1.0"

    async def resolve(self, candidates: list[Entity]) -> ResolutionResult:
        if not candidates:
            return ResolutionResult()

        buckets: dict[str, list[Entity]] = defaultdict(list)
        for entity in candidates:
            buckets[_canonical_key(entity)].append(entity)

        canonical: list[Entity] = []
        merges: list[MergeRecord] = []
        unresolved: list[Entity] = []
        now = datetime.now(UTC)

        for group in buckets.values():
            if len(group) == 1:
                # Single-occurrence entity — pass through unchanged, and
                # don't emit a merge record for a no-op.
                canonical.append(group[0])
                continue
            merged_aliases: list[str] = []
            for e in group:
                merged_aliases.append(e.canonical_name)
                merged_aliases.extend(e.aliases)
            unique_aliases = list(dict.fromkeys(merged_aliases))
            winner = group[0]
            merged_entity = winner.model_copy(
                update={"aliases": [a for a in unique_aliases if a != winner.canonical_name]}
            )
            canonical.append(merged_entity)
            merges.append(
                MergeRecord(
                    canonical_id=winner.id,
                    merged_ids=[e.id for e in group],
                    resolver_id=self.id,
                    resolver_version=self.version,
                    resolved_at=now,
                    reason="exact match on (type, casefolded canonical_name)",
                )
            )

        return ResolutionResult(entities=canonical, merges=merges, unresolved=unresolved)


__all__ = ["ExactMatchResolver", "Resolver"]
