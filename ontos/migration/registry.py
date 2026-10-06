"""MigrationRegistry — chain OntologyMigration instances across versions.

Operators register one migration per (from_version, to_version) edge;
the registry resolves a path via BFS when the target is more than one
hop away. Translation at read time runs each migration in order,
threading the Fact through — any step returning `None` drops the
fact; otherwise the output of step `k` is the input to step `k+1`.

**Strict mode.** When `strict=True`, every unresolved case (empty
source version, no path registered) returns `None` instead of
passing the fact through. Operators who want "the system refuses to
show you a fact it cannot vouch for under the current ontology"
enable this; the default (`strict=False`) is more forgiving — pass
through with a `MigrationWarning` — so pre-#34 facts stay visible
during the ingest-side rollout window.

The seam mirrors `SupersessionPolicy` / `Resolver`: no global state,
registry is a plain Python object the executor holds a reference to.
"""

from __future__ import annotations

from collections import deque

from ontos.migration.base import MigrationWarning, OntologyMigration
from ontos.ontology import Ontology
from ontos.runtime.models import Fact

_REGISTRY_ID = "ontos.migration.registry"


class MigrationRegistry:
    """Register OntologyMigration edges and translate facts across versions."""

    def __init__(self, *, strict: bool = False) -> None:
        # Each `(from, to)` edge holds a LIST of migrations because a
        # single version bump typically bundles several changes
        # (rename predicate X + deprecate predicate Y + tighten
        # cardinality on Z). All migrations on an edge apply in
        # registration order when the chain traverses that edge.
        self._edges: dict[tuple[str, str], list[OntologyMigration]] = {}
        self._strict = strict

    @property
    def strict(self) -> bool:
        return self._strict

    def register(self, migration: OntologyMigration) -> None:
        """Append `migration` to the `(from_version, to_version)` edge.

        Multiple migrations per edge are expected and apply in
        registration order. Re-registering a migration whose `id`
        already exists on the edge replaces it in-place (operators
        tweaking a migration mid-session) — identity-by-id, not by
        object. Different ids coexist and chain within the edge.
        """
        key = (migration.from_version, migration.to_version)
        bucket = self._edges.setdefault(key, [])
        for idx, existing in enumerate(bucket):
            if existing.id == migration.id:
                bucket[idx] = migration
                return
        bucket.append(migration)

    def path(self, from_v: str, to_v: str) -> list[OntologyMigration] | None:
        """Shortest version-hop chain from `from_v` to `to_v`.

        Returns the ordered list of EVERY migration that fires on the
        path (flattened across edges with multiple migrations). BFS
        chooses the shortest hop count between versions; migrations
        inside an edge apply in registration order.
        """
        if from_v == to_v:
            return []
        visited: set[str] = {from_v}
        queue: deque[tuple[str, list[OntologyMigration]]] = deque([(from_v, [])])
        while queue:
            current, chain = queue.popleft()
            for (src, dst), bucket in self._edges.items():
                if src != current or dst in visited:
                    continue
                new_chain = [*chain, *bucket]
                if dst == to_v:
                    return new_chain
                visited.add(dst)
                queue.append((dst, new_chain))
        return None

    async def migrate_fact(
        self,
        fact: Fact,
        target: Ontology,
    ) -> tuple[Fact | None, list[MigrationWarning]]:
        """Translate `fact` into `target`'s ontology.

        Returns `(migrated_fact_or_None, warnings)`.

        Routing:

        - Source version == target version → pass through, no warning.
        - Source version empty (pre-#34 fact) → pass through with a
          `"ontology-unstamped"` warning (non-strict) or drop (strict).
        - Target id differs from fact's `ontology_id` → unsupported
          cross-ontology translation; pass through with a
          `"cross-ontology-translation-unsupported"` warning
          (non-strict) or drop (strict). One registry serves one
          ontology family; cross-family migration is out of scope.
        - Path found → apply each migration in order; any step
          returning `None` drops the fact and emits one warning per
          dropped step so operators can trace which migration dropped it.
        - No path found → pass through with a `"no-migration-path"`
          warning (non-strict) or drop (strict).
        """
        warnings: list[MigrationWarning] = []
        source_id = fact.provenance.ontology_id
        source_version = fact.provenance.ontology_version

        if source_version == target.version and source_id in (target.id, ""):
            return fact, warnings

        if not source_version:
            warnings.append(
                MigrationWarning(
                    fact_id=fact.id,
                    from_version="",
                    to_version=target.version,
                    migration_id=_REGISTRY_ID,
                    reason="ontology-unstamped",
                )
            )
            return (None if self._strict else fact), warnings

        if source_id and source_id != target.id:
            warnings.append(
                MigrationWarning(
                    fact_id=fact.id,
                    from_version=source_version,
                    to_version=target.version,
                    migration_id=_REGISTRY_ID,
                    reason=(
                        f"cross-ontology-translation-unsupported "
                        f"(fact.ontology_id={source_id!r}, "
                        f"target.id={target.id!r})"
                    ),
                )
            )
            return (None if self._strict else fact), warnings

        chain = self.path(source_version, target.version)
        if chain is None:
            warnings.append(
                MigrationWarning(
                    fact_id=fact.id,
                    from_version=source_version,
                    to_version=target.version,
                    migration_id=_REGISTRY_ID,
                    reason="no-migration-path",
                )
            )
            return (None if self._strict else fact), warnings

        # Thread the fact through each step. Each migration sees the
        # same `target` ontology; the `source` ontology handle is a
        # best-effort reconstruction — we don't maintain a historical
        # ontology registry here, so we pass `target` on both sides.
        # Built-in migrations don't inspect `source` (predicate rename
        # etc. are self-contained); custom migrations that need the
        # original schema should carry it as internal state.
        current: Fact | None = fact
        for migration in chain:
            assert current is not None
            next_fact = await migration.migrate_fact(current, target, target)
            if next_fact is None:
                warnings.append(
                    MigrationWarning(
                        fact_id=fact.id,
                        from_version=migration.from_version,
                        to_version=migration.to_version,
                        migration_id=migration.id,
                        reason="migration-dropped-fact",
                    )
                )
                return None, warnings
            current = next_fact
        return current, warnings
