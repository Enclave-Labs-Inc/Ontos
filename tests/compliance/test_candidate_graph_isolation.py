"""M1 compliance: rejected candidates never enter trusted graph exports."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.exporters.cypher import render_cypher
from ontos.extraction import (
    CandidateProvenance,
    CandidateTriple,
    RejectedCandidate,
)
from ontos.storage.networkx_store import NetworkxStore


def _candidate() -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="candidate-secret-person",
            subject_type="Person",
            subject_canonical_name="Candidate Secret Person",
            predicate="candidate_secret_predicate",
            object_id="candidate-secret-company",
            object_type="Company",
            object_canonical_name="Candidate Secret Company",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="candidate-secret-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.9,
        ),
        acl_ref="acl:private",
    )


@pytest.mark.parametrize("include_all_acls", [False, True])
async def test_candidates_absent_from_graph_scan(
    include_all_acls: bool,
) -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    scan = await store.scan(
        allowed_acls=["acl:private"],
        include_all_acls=include_all_acls,
    )

    assert scan.entities == []
    assert scan.facts == []
    assert scan.merges == []

    serialized = scan.model_dump_json()

    for secret in (
        "candidate-secret-person",
        "candidate-secret-company",
        "candidate_secret_predicate",
        "candidate-secret-source",
    ):
        assert secret not in serialized


async def test_candidates_absent_from_privileged_cypher_export() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    scan = await store.scan(include_all_acls=True)
    output = render_cypher(scan)

    assert "RejectedCandidate" not in output
    assert "candidate-secret-person" not in output
    assert "candidate-secret-company" not in output
    assert "candidate_secret_predicate" not in output
    assert "candidate-secret-source" not in output


async def test_candidates_remain_isolated_after_store_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidates.pkl"

    writer = NetworkxStore(path=path)
    await writer.add_candidates([_candidate()])
    await writer.close()

    reader = NetworkxStore(path=path)

    # Persistence preserves the candidate separately.
    candidates = await reader.list_candidates(
        allowed_acls=["acl:private"],
    )
    assert len(candidates) == 1

    # But graph APIs and exports must remain completely unaware of it.
    scan = await reader.scan(include_all_acls=True)

    assert scan.entities == []
    assert scan.facts == []
    assert scan.merges == []

    output = render_cypher(scan)
    assert "candidate_secret_predicate" not in output

    await reader.close()


async def test_candidates_absent_from_every_graph_export_format() -> None:
    from ontos.exporters.graphml import render_graphml
    from ontos.exporters.jsonld import render_jsonld
    from ontos.exporters.svg import render_svg

    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    scan = await store.scan(include_all_acls=True)

    outputs = {
        "cypher": render_cypher(scan),
        "graphml": render_graphml(scan),
        "jsonld": render_jsonld(scan),
        "svg": render_svg(scan),
    }

    for format_name, output in outputs.items():
        for secret in (
            "candidate-secret-person",
            "candidate-secret-company",
            "candidate_secret_predicate",
            "candidate-secret-source",
            "Candidate Secret Person",
            "Candidate Secret Company",
        ):
            assert secret not in output, f"{format_name} exposed rejected candidate data"


async def test_candidates_absent_from_search_and_traversal() -> None:
    """Rejected candidates must never enter trusted search or traversal."""
    store = NetworkxStore()
    await store.add_candidates([_candidate()])

    # Searching for the rejected predicate must find no trusted facts.
    hits = await store.search(
        "candidate_secret_predicate",
        allowed_acls=["acl:private"],
    )
    assert hits == []

    # Even a traversal starting from a raw candidate entity ID
    # must not discover the rejected relationship.
    relationships = await store.traverse(
        "candidate-secret-person",
        depth=2,
        allowed_acls=["acl:private"],
    )
    assert list(relationships) == []

    # Verify that a candidate-only store contains no trusted nodes.
    scan = await store.scan(include_all_acls=True)
    assert scan.entities == []
    assert scan.facts == []
    assert scan.merges == []
