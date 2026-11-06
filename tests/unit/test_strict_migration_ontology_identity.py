"""Strict migrations require an identifiable ontology family."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ontos.migration import MigrationRegistry
from ontos.migration.builtins import IdentityMigration
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.runtime.models import Confidence, Fact, Provenance


def _ontology() -> Ontology:
    return Ontology(
        id="ontos.test",
        version="0.2",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[RelationType(label="works_at")],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            )
        ],
    )


def _fact(version: str, ontology_id: str) -> Fact:
    now = datetime.now(UTC)
    return Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="document.txt",
            extractor_id="test",
            extractor_version="1",
            ontology_id=ontology_id,
            ontology_version=version,
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
        ),
        t_valid=now,
        ingested_at=now,
    )


def _registry(strict: bool) -> MigrationRegistry:
    registry = MigrationRegistry(strict=strict)
    registry.register(
        IdentityMigration(
            from_version="0.1",
            to_version="0.2",
            approval_query_id=uuid4(),
        )
    )
    return registry


@pytest.mark.parametrize("version", ["0.1", "0.2"])
async def test_strict_rejects_missing_ontology_id(
    version: str,
) -> None:
    fact = _fact(version, ontology_id="")

    migrated, warnings = await _registry(strict=True).migrate_fact(
        fact,
        _ontology(),
    )

    assert migrated is None
    assert len(warnings) == 1
    assert warnings[0].reason == "ontology-id-unstamped"


@pytest.mark.parametrize("version", ["0.1", "0.2"])
async def test_strict_accepts_correct_ontology_id(
    version: str,
) -> None:
    fact = _fact(version, ontology_id="ontos.test")

    migrated, warnings = await _registry(strict=True).migrate_fact(
        fact,
        _ontology(),
    )

    assert migrated is fact
    assert warnings == []


@pytest.mark.parametrize("version", ["0.1", "0.2"])
async def test_non_strict_preserves_legacy_missing_id_behavior(
    version: str,
) -> None:
    fact = _fact(version, ontology_id="")

    migrated, warnings = await _registry(strict=False).migrate_fact(
        fact,
        _ontology(),
    )

    assert migrated is fact
    assert warnings == []
