"""Ship-with-the-box ontology migrations.

Three built-ins cover the most common schema evolutions:

- `PredicateRename` — rename a relation (e.g. `works_at` → `employed_by`).
- `PredicateDeprecate` — remove a relation; facts under it drop out.
- `CardinalityTighten` — flag facts whose target-ontology cardinality
  the stored distribution violates. Pass-through at the Fact level;
  operators consume the audit warnings to decide whether to
  retroactively close.

Entity-type widening and other type-system evolutions are NOT
represented here: a `Fact` only carries `subject_id` / `object_id`
strings, so widening the type a node is labeled with is an
Ontology-side concern (and happens at entity re-ingest / resolver
runs). The migration seam translates **facts**, not entity typings.
"""

from __future__ import annotations

from ontos.ontology import Ontology
from ontos.runtime.models import Fact


class PredicateRename:
    """`fact.predicate: from_predicate → to_predicate` at read time."""

    id: str = "ontos.migration.predicate-rename"

    def __init__(
        self,
        *,
        from_version: str,
        to_version: str,
        from_predicate: str,
        to_predicate: str,
    ) -> None:
        self._from_version = from_version
        self._to_version = to_version
        self._from_predicate = from_predicate
        self._to_predicate = to_predicate

    @property
    def from_version(self) -> str:
        return self._from_version

    @property
    def to_version(self) -> str:
        return self._to_version

    async def migrate_fact(
        self,
        fact: Fact,
        source: Ontology,
        target: Ontology,
    ) -> Fact | None:
        if fact.predicate != self._from_predicate:
            return fact
        return fact.model_copy(update={"predicate": self._to_predicate})


class PredicateDeprecate:
    """Drop any fact whose predicate was removed between versions."""

    id: str = "ontos.migration.predicate-deprecate"

    def __init__(
        self,
        *,
        from_version: str,
        to_version: str,
        predicate: str,
    ) -> None:
        self._from_version = from_version
        self._to_version = to_version
        self._predicate = predicate

    @property
    def from_version(self) -> str:
        return self._from_version

    @property
    def to_version(self) -> str:
        return self._to_version

    async def migrate_fact(
        self,
        fact: Fact,
        source: Ontology,
        target: Ontology,
    ) -> Fact | None:
        if fact.predicate == self._predicate:
            return None
        return fact


class CardinalityTighten:
    """Pass-through migration that flags target-ontology cardinality violations.

    The target ontology may have tightened a relation from
    `many_to_many` to `many_to_one` (or similar). This migration
    doesn't close any facts — doing so would violate bitemporal
    immutability — but it surfaces the mismatch so an operator can
    export, review, and manually close the surviving duplicates.

    The actual "would this fact violate target cardinality?" check
    requires looking at the full fact distribution for a (subject,
    predicate) pair in the store; a per-fact migration can't answer
    that, so this migration always returns the fact unchanged. The
    class exists so operators can wire it into the registry chain
    for audit stamping — a future enhancement can wire a
    distribution-aware checker through the executor's seam.
    """

    id: str = "ontos.migration.cardinality-tighten"

    def __init__(
        self,
        *,
        from_version: str,
        to_version: str,
        predicate: str,
    ) -> None:
        self._from_version = from_version
        self._to_version = to_version
        self._predicate = predicate

    @property
    def from_version(self) -> str:
        return self._from_version

    @property
    def to_version(self) -> str:
        return self._to_version

    @property
    def predicate(self) -> str:
        return self._predicate

    async def migrate_fact(
        self,
        fact: Fact,
        source: Ontology,
        target: Ontology,
    ) -> Fact | None:
        return fact
