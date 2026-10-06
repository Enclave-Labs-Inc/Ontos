"""#34 — MigrationRegistry unit tests.

Pins the Protocol contract: registry chains migrations across
versions, drops facts on strict / passes with warnings otherwise,
and preserves bitemporal immutability by returning new Fact objects
instead of mutating stored ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ontos.migration import (
    CardinalityTighten,
    MigrationRegistry,
    PredicateDeprecate,
    PredicateRename,
)
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.runtime.models import Confidence, Fact, Provenance


def _target_v02() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.2",
        entity_types=[EntityType(label="Person"), EntityType(label="Company")],
        relation_types=[RelationType(label="employed_by")],
        patterns=[Pattern(subject_type="Person", predicate="employed_by", object_type="Company")],
    )


def _target_v03() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.3",
        entity_types=[EntityType(label="Person"), EntityType(label="Company")],
        relation_types=[RelationType(label="employment")],
        patterns=[Pattern(subject_type="Person", predicate="employment", object_type="Company")],
    )


def _fact(predicate: str, *, ontology_version: str = "0.1") -> Fact:
    now = datetime.now(UTC)
    return Fact(
        id=uuid4(),
        subject_id="person:alice",
        predicate=predicate,
        object_id="company:acme",
        provenance=Provenance(
            source_id="doc1",
            extractor_id="test",
            extractor_version="0.0.0",
            ontology_id="ontos.starter",
            ontology_version=ontology_version,
            confidence=Confidence.EXTRACTED,
            confidence_score=0.9,
        ),
        t_valid=now,
        ingested_at=now,
    )


async def test_direct_edge_translates_predicate() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    stored = _fact("works_at")
    migrated, warnings = await reg.migrate_fact(stored, _target_v02())

    assert migrated is not None
    assert migrated.predicate == "employed_by"
    # Stored fact bitemporally immutable — only the projection changed.
    assert stored.predicate == "works_at"
    assert warnings == []


async def test_chained_path_applies_migrations_in_order() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    reg.register(
        PredicateRename(
            from_version="0.2",
            to_version="0.3",
            from_predicate="employed_by",
            to_predicate="employment",
        )
    )
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), _target_v03())

    assert migrated is not None
    assert migrated.predicate == "employment"
    assert warnings == []


async def test_predicate_rename_leaves_unrelated_facts_alone() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    migrated, _ = await reg.migrate_fact(_fact("acquired"), _target_v02())
    assert migrated is not None
    assert migrated.predicate == "acquired"


async def test_predicate_deprecate_drops_matching_facts() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateDeprecate(
            from_version="0.1",
            to_version="0.2",
            predicate="works_at",
        )
    )
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), _target_v02())

    assert migrated is None
    assert len(warnings) == 1
    assert warnings[0].reason == "migration-dropped-fact"
    assert warnings[0].migration_id == "ontos.migration.predicate-deprecate"


async def test_cardinality_tighten_is_passthrough_for_now() -> None:
    reg = MigrationRegistry()
    reg.register(
        CardinalityTighten(
            from_version="0.1",
            to_version="0.2",
            predicate="works_at",
        )
    )
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), _target_v02())
    assert migrated is not None
    assert migrated.predicate == "works_at"
    # Pass-through contract: no per-fact warning. Audit-level
    # distribution checking is a future extension that lives at the
    # executor's seam, not inside the per-fact migration.
    assert warnings == []


async def test_unstamped_fact_passes_through_with_warning_non_strict() -> None:
    reg = MigrationRegistry()  # strict=False default
    stored = _fact("works_at", ontology_version="")
    migrated, warnings = await reg.migrate_fact(stored, _target_v02())

    assert migrated is stored
    assert len(warnings) == 1
    assert warnings[0].reason == "ontology-unstamped"


async def test_unstamped_fact_dropped_under_strict() -> None:
    reg = MigrationRegistry(strict=True)
    stored = _fact("works_at", ontology_version="")
    migrated, warnings = await reg.migrate_fact(stored, _target_v02())

    assert migrated is None
    assert len(warnings) == 1
    assert warnings[0].reason == "ontology-unstamped"


async def test_no_path_passes_through_with_warning_non_strict() -> None:
    reg = MigrationRegistry()
    # No migration registered for 0.1 → 0.2.
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), _target_v02())

    assert migrated is not None
    assert migrated.predicate == "works_at"
    assert len(warnings) == 1
    assert warnings[0].reason == "no-migration-path"


async def test_no_path_dropped_under_strict() -> None:
    reg = MigrationRegistry(strict=True)
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), _target_v02())

    assert migrated is None
    assert warnings[0].reason == "no-migration-path"


async def test_same_version_pass_through_no_warning() -> None:
    reg = MigrationRegistry(strict=True)  # even strict permits identity
    stored = _fact("works_at", ontology_version="0.2")
    migrated, warnings = await reg.migrate_fact(stored, _target_v02())

    assert migrated is stored
    assert warnings == []


async def test_cross_ontology_translation_is_refused() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    # Target id differs from fact's ontology_id — a cross-family hop
    # the registry explicitly refuses.
    foreign_target = _target_v02().model_copy(update={"id": "customer.finops"})
    migrated, warnings = await reg.migrate_fact(_fact("works_at"), foreign_target)

    assert migrated is not None  # non-strict pass-through
    assert len(warnings) == 1
    assert "cross-ontology-translation-unsupported" in warnings[0].reason


async def test_multiple_migrations_coexist_on_same_edge() -> None:
    # One version bump typically bundles multiple changes — rename
    # some predicates, deprecate others. All migrations on an edge
    # apply in registration order.
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="works_at",
            to_predicate="employed_by",
        )
    )
    reg.register(
        PredicateDeprecate(from_version="0.1", to_version="0.2", predicate="acquired"),
    )
    # The works_at fact survives as employed_by.
    migrated, _ = await reg.migrate_fact(_fact("works_at"), _target_v02())
    assert migrated is not None
    assert migrated.predicate == "employed_by"
    # The acquired fact drops.
    migrated_drop, warnings = await reg.migrate_fact(_fact("acquired"), _target_v02())
    assert migrated_drop is None
    assert any(w.migration_id == "ontos.migration.predicate-deprecate" for w in warnings)


async def test_register_same_id_replaces_in_place() -> None:
    reg = MigrationRegistry()
    first = PredicateRename(
        from_version="0.1",
        to_version="0.2",
        from_predicate="works_at",
        to_predicate="old_name",
    )
    second = PredicateRename(
        from_version="0.1",
        to_version="0.2",
        from_predicate="works_at",
        to_predicate="employed_by",
    )
    reg.register(first)
    reg.register(second)  # same id, replaces first
    migrated, _ = await reg.migrate_fact(_fact("works_at"), _target_v02())
    assert migrated is not None
    assert migrated.predicate == "employed_by"


async def test_path_returns_none_for_unreachable_target() -> None:
    reg = MigrationRegistry()
    reg.register(
        PredicateRename(
            from_version="0.1",
            to_version="0.2",
            from_predicate="x",
            to_predicate="y",
        )
    )
    # 0.3 is not reachable from 0.1.
    assert reg.path("0.1", "0.3") is None


async def test_path_returns_empty_for_identity() -> None:
    reg = MigrationRegistry()
    assert reg.path("0.1", "0.1") == []
