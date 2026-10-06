"""OntologyMigration Protocol + MigrationWarning audit record.

A migration translates one `Fact` from a source ontology version to
a target version. Instances are stateless and reused across every
fact that passes through them. The Protocol mirrors
`ontos.supersession.SupersessionPolicy`'s shape — `id`, version
pair, async per-item method — so plug-in implementations stay
consistent across the two Protocols.

The underlying stored fact is **never** mutated. A migration returns
either a NEW `Fact` (via `fact.model_copy(update=...)`) that
represents the target-version projection, or `None` to drop the fact
from the read-side view. Bitemporal immutability of the stored fact
is a load-bearing invariant (CLAUDE.md): the migration seam preserves
it by producing throw-away projections.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ontos.ontology import Ontology
from ontos.runtime.models import Fact


class MigrationWarning(BaseModel):
    """One audit breadcrumb for a migration event.

    Emitted by `MigrationRegistry.migrate_fact` whenever a fact
    cannot be translated cleanly: no chain registered, the source
    ontology version is empty (pre-#34 fact), or a built-in
    migration surfaced a target-ontology violation.

    `fact_id` ties the warning back to the stored fact; `migration_id`
    names the specific `OntologyMigration` instance (or the sentinel
    `ontos.migration.registry` when the registry itself emitted it,
    e.g. no-path cases).
    """

    fact_id: UUID
    from_version: str
    to_version: str
    migration_id: str
    reason: str

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class OntologyMigration(Protocol):
    """Every migration implementation satisfies this surface."""

    @property
    def id(self) -> str:
        """Stable identifier (e.g. ``"ontos.migration.predicate-rename"``)."""
        ...

    @property
    def from_version(self) -> str:
        """Source ontology version this migration understands."""
        ...

    @property
    def to_version(self) -> str:
        """Target ontology version this migration produces."""
        ...

    async def migrate_fact(
        self,
        fact: Fact,
        source: Ontology,
        target: Ontology,
    ) -> Fact | None:
        """Translate ``fact`` from ``source`` to ``target``.

        - Return a new ``Fact`` (typically via ``fact.model_copy``)
          when the migration applies.
        - Return the input ``fact`` unchanged when this migration
          doesn't touch it (e.g. a `PredicateRename` whose
          `from_predicate` doesn't match this fact's predicate).
        - Return ``None`` to drop the fact (e.g. the predicate was
          deprecated between versions).

        Never mutate the input ``fact`` — Pydantic frozen models
        make that impossible, but preserving the invariant explicitly
        here matters for Protocol conformance.
        """
        ...
