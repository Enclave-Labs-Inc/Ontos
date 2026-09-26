"""M4.a — resolver cascade tests. Pins merge-lineage preservation."""

from __future__ import annotations

import pytest

from ontos.resolver import (
    CascadeResolver,
    ExactMatchResolver,
    MergeRecord,
    ResolutionResult,
    Resolver,
)
from ontos.runtime.models import Confidence, Entity, Provenance


def _entity(entity_id: str, type_: str, canonical_name: str) -> Entity:
    return Entity(
        id=entity_id,
        type=type_,
        canonical_name=canonical_name,
        provenance=Provenance(
            source_id="t",
            extractor_id="t",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )


def test_exact_match_resolver_conforms_to_protocol() -> None:
    assert isinstance(ExactMatchResolver(), Resolver)


async def test_empty_input_returns_empty_result() -> None:
    r = ExactMatchResolver()
    result = await r.resolve([])
    assert result == ResolutionResult()


async def test_single_candidate_passes_through_with_no_merge_record() -> None:
    r = ExactMatchResolver()
    entities = [_entity("e:1", "Person", "Alice")]
    result = await r.resolve(entities)
    assert len(result.entities) == 1
    assert result.entities[0].id == "e:1"
    assert result.merges == []


async def test_duplicates_by_normalized_name_merge_and_emit_record() -> None:
    r = ExactMatchResolver()
    candidates = [
        _entity("e:1", "Person", "Alice Smith"),
        _entity("e:2", "Person", "alice smith"),
        _entity("e:3", "Person", "  ALICE SMITH  "),
    ]
    result = await r.resolve(candidates)
    assert len(result.entities) == 1
    canonical = result.entities[0]
    assert canonical.id == "e:1"  # winner is the first seen
    assert len(result.merges) == 1
    record = result.merges[0]
    assert record.canonical_id == "e:1"
    assert set(record.merged_ids) == {"e:1", "e:2", "e:3"}
    assert record.resolver_id == "ontos.resolver.exact-match"


async def test_different_types_do_not_merge_even_with_same_name() -> None:
    r = ExactMatchResolver()
    candidates = [
        _entity("e:1", "Person", "Acme"),
        _entity("e:2", "Company", "Acme"),
    ]
    result = await r.resolve(candidates)
    assert len(result.entities) == 2
    assert result.merges == []


async def test_merge_preserves_aliases() -> None:
    r = ExactMatchResolver()
    e1 = Entity(
        id="e:1",
        type="Person",
        canonical_name="Alice Smith",
        aliases=["A. Smith"],
        provenance=Provenance(
            source_id="t",
            extractor_id="t",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    e2 = Entity(
        id="e:2",
        type="Person",
        canonical_name="alice smith",
        aliases=["alice"],
        provenance=Provenance(
            source_id="t",
            extractor_id="t",
            extractor_version="0.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
    )
    result = await r.resolve([e1, e2])
    assert len(result.entities) == 1
    winner = result.entities[0]
    # Merged aliases include both e1's aliases and e2's raw name/aliases
    # (minus the canonical_name itself).
    assert "A. Smith" in winner.aliases
    assert "alice" in winner.aliases
    assert "alice smith" in winner.aliases  # e2's canonical becomes an alias


async def test_merge_record_is_frozen() -> None:
    from datetime import UTC, datetime

    import pydantic

    rec = MergeRecord(
        canonical_id="e:1",
        merged_ids=["e:1", "e:2"],
        resolver_id="r",
        resolver_version="0",
        resolved_at=datetime.now(UTC),
    )
    with pytest.raises(pydantic.ValidationError):
        rec.canonical_id = "e:other"  # type: ignore[misc]


async def test_cascade_requires_at_least_one_tier() -> None:
    with pytest.raises(ValueError):
        CascadeResolver([])


async def test_cascade_with_single_tier_matches_that_tier() -> None:
    solo = ExactMatchResolver()
    cascade = CascadeResolver([solo])
    entities = [
        _entity("e:1", "Person", "Alice"),
        _entity("e:2", "Person", "alice"),
    ]
    result = await cascade.resolve(entities)
    assert len(result.entities) == 1
    assert len(result.merges) == 1


async def test_cascade_id_and_version_compose_tier_metadata() -> None:
    cascade = CascadeResolver([ExactMatchResolver(), ExactMatchResolver()])
    assert "ontos.resolver.exact-match" in cascade.id
    assert cascade.version  # non-empty


class _NoopResolver:
    """Passes everything through as unresolved — for testing cascade forwarding."""

    id: str = "noop"
    version: str = "0"

    async def resolve(self, candidates: list[Entity]) -> ResolutionResult:
        return ResolutionResult(entities=[], merges=[], unresolved=candidates)


async def test_cascade_forwards_unresolved_to_next_tier() -> None:
    cascade = CascadeResolver([_NoopResolver(), ExactMatchResolver()])
    entities = [
        _entity("e:1", "Person", "Alice"),
        _entity("e:2", "Person", "alice"),
    ]
    result = await cascade.resolve(entities)
    # Noop passed both to ExactMatch, which merged them.
    assert len(result.entities) == 1
    assert len(result.merges) == 1
    assert result.merges[0].resolver_id == "ontos.resolver.exact-match"


async def test_cascade_unresolved_at_the_end_falls_through_untouched() -> None:
    cascade = CascadeResolver([_NoopResolver()])
    entities = [
        _entity("e:1", "Person", "Alice"),
        _entity("e:2", "Company", "Acme"),
    ]
    result = await cascade.resolve(entities)
    # Nothing merged, but no entities lost.
    assert len(result.entities) == 2
    assert result.merges == []
    assert result.unresolved == []  # cascade drains the queue


async def test_merge_lineage_recoverable_from_merge_records() -> None:
    """Audit requirement (M1.d): given a canonical entity id, the audit
    trail must let you recover every original id that got merged into it."""
    r = ExactMatchResolver()
    entities = [
        _entity("e:1", "Person", "Alice"),
        _entity("e:2", "Person", "alice"),
        _entity("e:3", "Person", "ALICE"),
        _entity("e:4", "Company", "Acme"),  # unrelated
    ]
    result = await r.resolve(entities)
    # Rebuild the lineage from merges.
    lineage: dict[str, list[str]] = {}
    for m in result.merges:
        lineage[m.canonical_id] = list(m.merged_ids)
    assert set(lineage.get("e:1", [])) == {"e:1", "e:2", "e:3"}
