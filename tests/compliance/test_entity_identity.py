"""Gate-blocking: one real-world entity is one graph entity (#121, #122).

The model invents its own entity ids, inconsistently (#121 saw "2" and "4"
for one "Acme Corp" in one document and "Company:Acme Corp" in another).
Stored as-is they split one entity into several nodes, and `ask` refused to
guess between them. Entity ids now come from (type, name), scoped by ACL,
and the executor treats same-identity candidates the caller can see as one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ontos.executor import DeterministicExecutor
from ontos.extraction import LlmExtractor, RawTriple
from ontos.extraction.identity import acl_scoped_entity_id, entity_identity_id
from ontos.ingest import TextConnector
from ontos.ontology import load_ontology
from ontos.pipeline import ErrorPolicy, IngestPipeline
from ontos.planner import Plan, SeedByEntity, TraversalStep
from ontos.resolver import ExactMatchResolver
from ontos.storage.networkx_store import NetworkxStore

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
ONTOLOGY = load_ontology(STARTER)

TEAM_2024 = "Dana Lee joined Acme Corp as Chief Technology Officer in January 2024."
UPDATE_2026 = "In March 2026, Sam Ortiz replaced Dana Lee as CTO of Acme Corp."


def _t(
    s_id: str, s_type: str, s_name: str, pred: str, o_id: str, o_type: str, o_name: str
) -> RawTriple:
    return RawTriple(
        subject_id=s_id,
        subject_type=s_type,
        subject_canonical_name=s_name,
        predicate=pred,
        object_id=o_id,
        object_type=o_type,
        object_canonical_name=o_name,
        llm_confidence=0.95,
    )


class _ScriptedLLM:
    id = "scripted"
    version = "1"

    def __init__(self, script: dict[str, list[RawTriple]]) -> None:
        self._script = script

    async def structured_extract(self, text: str, ontology: Any) -> list[RawTriple]:
        return self._script[text]


# The id mess from #121, verbatim in shape.
SCRIPT = {
    TEAM_2024: [
        _t(
            "Person:Dana Lee",
            "Person",
            "Dana Lee",
            "works_at",
            "Company:Acme Corp",
            "Company",
            "Acme Corp",
        )
    ],
    UPDATE_2026: [
        _t("1", "Person", "Sam Ortiz", "works_at", "2", "Company", "Acme Corp"),
        _t("3", "Person", "Dana Lee", "works_at", "4", "Company", "Acme Corp"),
    ],
}


async def _ingest(store: NetworkxStore, acl_by_source: dict[str, str] | None = None) -> Any:
    return await IngestPipeline(
        connector=TextConnector(
            {"2024-team.md": TEAM_2024, "2026-update.md": UPDATE_2026},
            acl_ref_by_source=acl_by_source or {},
        ),
        extractor=LlmExtractor(_ScriptedLLM(SCRIPT), ONTOLOGY),
        resolver=ExactMatchResolver(),
        store=store,
        ontology=ONTOLOGY,
        error_policy=ErrorPolicy.SKIP_AND_LOG,
    ).run()


def _who_works_at_acme() -> Plan:
    return Plan.build(
        seed=SeedByEntity(entity_id="Acme Corp"),
        steps=[TraversalStep(relations=["works_at"], depth=1, direction="in")],
        ontology=ONTOLOGY,
    )


async def test_mixed_model_ids_converge_to_one_entity() -> None:
    store = NetworkxStore()

    report = await _ingest(store)

    assert report.errors == []
    acme_ids = {e.id for e in (await store.scan()).entities if e.canonical_name == "Acme Corp"}
    assert acme_ids == {entity_identity_id("Company", "Acme Corp")}


async def test_who_works_at_acme_answers_with_sources() -> None:
    store = NetworkxStore()
    await _ingest(store)

    result = await DeterministicExecutor().execute(_who_works_at_acme(), store)

    answers = {(h.fact.subject_id, h.fact.provenance.source_id) for h in result.hits}
    dana, sam = entity_identity_id("Person", "Dana Lee"), entity_identity_id("Person", "Sam Ortiz")
    assert (sam, "2026-update.md") in answers
    assert {subject for subject, _ in answers} == {dana, sam}
    assert not any("refusing to guess" in w for w in result.warnings)


async def test_same_name_under_two_acls_stays_two_entities() -> None:
    store = NetworkxStore()

    report = await _ingest(store, {"2026-update.md": "acl:finance"})

    # No cross-ACL conflict and nothing dropped: separate scoped nodes.
    assert report.errors == []
    public = entity_identity_id("Company", "Acme Corp")
    finance = acl_scoped_entity_id(public, "acl:finance")
    ids = {e.id for e in (await store.scan(include_all_acls=True)).entities}
    assert {public, finance} <= ids
    assert not any("acl:finance" in entity_id for entity_id in ids)


async def test_consolidation_never_crosses_into_acls_the_caller_cannot_see() -> None:
    store = NetworkxStore()
    await _ingest(store, {"2026-update.md": "acl:finance"})
    executor = DeterministicExecutor()

    public_only = await executor.execute(_who_works_at_acme(), store, allowed_acls=[])
    with_finance = await executor.execute(_who_works_at_acme(), store, allowed_acls=["acl:finance"])

    assert {h.fact.provenance.source_id for h in public_only.hits} == {"2024-team.md"}
    assert {h.fact.provenance.source_id for h in with_finance.hits} == {
        "2024-team.md",
        "2026-update.md",
    }
    assert not any("acl:finance" in w for w in public_only.warnings)


async def test_empty_entities_are_rejected_not_stored() -> None:
    """#122: a triple with an empty endpoint never becomes an `''` entity."""
    text = "Dana Lee joined Acme Corp."
    script = {
        text: [
            _t("x", "Person", "Dana Lee", "works_at", "y", "Company", "Acme Corp"),
            _t("y", "Company", "Acme Corp", "acquired", "", "Company", ""),
            _t("y", "Company", "Acme Corp", "acquired", "z", "", "Beta"),
        ]
    }
    store = NetworkxStore()

    report = await IngestPipeline(
        connector=TextConnector({"doc.md": text}),
        extractor=LlmExtractor(_ScriptedLLM(script), ONTOLOGY),
        resolver=None,
        store=store,
        ontology=ONTOLOGY,
    ).run()

    scan = await store.scan()
    assert report.facts_written == 1
    assert all(e.id.strip() and e.canonical_name for e in scan.entities)
    assert report.warnings.count("Triple rejected: empty entity id, name or type.") == 2
    assert text not in " ".join(report.warnings)
