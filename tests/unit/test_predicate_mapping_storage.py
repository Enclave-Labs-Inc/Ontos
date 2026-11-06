"""M3-1 regression tests for predicate-mapping provenance persistence."""

from datetime import UTC, datetime
from pathlib import Path

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.neo4j_store import _edge_props_to_fact, _fact_to_edge_props
from ontos.storage.networkx_store import NetworkxStore


def _mapped_fact() -> Fact:
    now = datetime.now(UTC)

    return Fact(
        subject_id="person:alice",
        predicate="works_at",
        object_id="company:acme",
        provenance=Provenance(
            source_id="employment.txt",
            extractor_id="test-extractor",
            extractor_version="1.0",
            ontology_id="starter",
            ontology_version="2.0.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.95,
            mapped_from_predicate="employed_by",
        ),
        t_valid=now,
        ingested_at=now,
    )


def test_neo4j_mapping_provenance_round_trip() -> None:
    fact = _mapped_fact()

    props = _fact_to_edge_props(fact)

    assert props["predicate"] == "works_at"
    assert props["prov_mapped_from_predicate"] == "employed_by"

    restored = _edge_props_to_fact(
        fact.subject_id,
        fact.object_id,
        props,
    )

    assert restored == fact
    assert restored.provenance.mapped_from_predicate == "employed_by"


def test_neo4j_legacy_edge_without_mapping_property() -> None:
    fact = _mapped_fact()
    props = _fact_to_edge_props(fact)

    # Simulate an edge created before M3-1.
    props.pop("prov_mapped_from_predicate")

    restored = _edge_props_to_fact(
        fact.subject_id,
        fact.object_id,
        props,
    )

    assert restored.predicate == "works_at"
    assert restored.provenance.mapped_from_predicate is None
    assert restored.provenance.ontology_version == "2.0.0"


async def test_networkx_mapping_provenance_survives_reopen(
    tmp_path: Path,
) -> None:
    fact = _mapped_fact()
    storage_path = tmp_path / "mapped-facts.pkl"

    writer = NetworkxStore(path=storage_path)
    await writer.add_fact(fact)
    await writer.close()

    assert storage_path.exists()

    reader = NetworkxStore(path=storage_path)
    try:
        restored = await reader.get_fact(fact.id)

        assert restored == fact
        assert restored is not None
        assert restored.provenance.mapped_from_predicate == "employed_by"
        assert restored.predicate == "works_at"
    finally:
        await reader.close()
