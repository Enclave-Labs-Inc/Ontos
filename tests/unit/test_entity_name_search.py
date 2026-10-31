"""#82: entities must be findable by canonical name and alias, not only by
their internal id, without leaking names of entities the caller can't see."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _provenance() -> Provenance:
    return Provenance(
        source_id="company.txt",
        extractor_id="test",
        extractor_version="1",
        confidence=Confidence.EXTRACTED,
        confidence_score=0.95,
    )


def _entity(entity_id: str, type_: str, name: str, aliases: list[str] | None = None) -> Entity:
    return Entity(
        id=entity_id,
        type=type_,
        canonical_name=name,
        aliases=aliases or [],
        provenance=_provenance(),
    )


def _works_at(person: str, company: str, acl_ref: str | None = None) -> Fact:
    return Fact(
        id=uuid4(),
        subject_id=person,
        predicate="works_at",
        object_id=company,
        provenance=_provenance(),
        t_valid=datetime(2025, 1, 1, tzinfo=UTC),
        ingested_at=datetime.now(UTC),
        acl_ref=acl_ref,
    )


async def _atlas_graph() -> NetworkxStore:
    """The issue's graph: names live on entities, ids are opaque."""
    store = NetworkxStore()
    await store.upsert_entity(_entity("0", "Person", "Maya Chen"))
    await store.upsert_entity(_entity("1", "Person", "Daniel Foster"))
    await store.upsert_entity(_entity("2", "Company", "Atlas Robotics", ["Atlas"]))
    await store.add_fact(_works_at("0", "2"))
    await store.add_fact(_works_at("1", "2"))
    return store


async def test_search_matches_canonical_name() -> None:
    store = await _atlas_graph()

    hits = await store.search("Atlas Robotics")

    assert sorted(f.subject_id for f in hits) == ["0", "1"]


async def test_search_matches_alias_and_person_name() -> None:
    store = await _atlas_graph()

    assert len(await store.search("atlas")) == 2
    assert [f.subject_id for f in await store.search("Maya Chen")] == ["0"]


async def test_search_still_matches_ids_and_predicates() -> None:
    store = await _atlas_graph()

    assert len(await store.search("works_at")) == 2
    assert len(await store.search("2")) == 2


async def test_search_does_not_match_names_of_invisible_entities() -> None:
    """A public fact can point at an entity a restricted document later
    upgraded. Its name is restricted content and must not be matchable."""
    store = NetworkxStore()
    await store.upsert_entity(_entity("p", "Person", "Public Person"))
    await store.add_fact(_works_at("p", "c"))
    # Public raw node first, then a restricted document claims it (None -> X).
    await store.upsert_entity(_entity("c", "Company", "Secret Corp"), acl_ref="acl:hr")

    assert await store.search("Secret Corp") == []
    assert await store.search("Secret Corp", allowed_acls=[]) == []
    assert len(await store.search("Secret Corp", allowed_acls=["acl:hr"])) == 1
    # The whole fact is hidden, not just the restricted name: its other
    # endpoint is invisible, and the fact would otherwise expose "c".
    assert await store.search("Public Person") == []
    assert len(await store.search("Public Person", allowed_acls=["acl:hr"])) == 1


async def test_search_entities_resolves_names_with_acl() -> None:
    store = await _atlas_graph()
    await store.upsert_entity(_entity("9", "Company", "Atlas Secret Lab"), acl_ref="acl:hr")
    await store.add_fact(_works_at("0", "9", acl_ref="acl:hr"))

    visible = await store.search_entities("atlas")
    assert [e.id for e in visible] == ["2"]

    with_hr = await store.search_entities("atlas", allowed_acls=["acl:hr"])
    assert sorted(e.id for e in with_hr) == ["2", "9"]

    assert [e.id for e in await store.search_entities("ATLAS ROBOTICS")] == ["2"]


async def test_search_entities_hides_entities_known_only_via_forbidden_facts() -> None:
    """Raw nodes created by add_fact have no ACL of their own. An entity
    whose every fact is forbidden must not be matchable by name or id."""
    store = NetworkxStore()
    await store.add_fact(_works_at("Classified Person", "Hidden Lab", acl_ref="acl:secret"))

    assert await store.search_entities("Hidden Lab") == []
    assert await store.search_entities("Hidden Lab", allowed_acls=["acl:public"]) == []
    found = await store.search_entities("Hidden Lab", allowed_acls=["acl:secret"])
    assert [e.id for e in found] == ["Hidden Lab"]


async def test_search_entities_respects_as_of() -> None:
    store = await _atlas_graph()
    before_any_fact = datetime(2000, 1, 1, tzinfo=UTC)

    assert await store.search_entities("Atlas Robotics", as_of=before_any_fact) == []
    assert [e.id for e in await store.search_entities("Atlas Robotics")] == ["2"]


async def test_issue_82_who_works_at_atlas_robotics_end_to_end() -> None:
    """The issue's query, through the executor, for both seed kinds."""
    from pathlib import Path

    from ontos.executor import DeterministicExecutor
    from ontos.ontology import load_ontology
    from ontos.planner import Plan, SeedByEntity, SeedByKeyword, TraversalStep

    ontology = load_ontology(
        Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
    )
    store = await _atlas_graph()
    step = TraversalStep(relations=["works_at"], depth=1, direction="in")

    for seed in (
        SeedByKeyword(keyword="Atlas Robotics", k=5),
        SeedByEntity(entity_id="Atlas Robotics"),
    ):
        plan = Plan.build(seed=seed, steps=[step], ontology=ontology)
        result = await DeterministicExecutor().execute(plan, store)

        assert sorted(hit.fact.subject_id for hit in result.hits) == ["0", "1"], seed
        assert not any("produced no entities" in w for w in result.warnings), seed
