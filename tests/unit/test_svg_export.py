"""#32: SVG graph exporter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from ontos.exporters.svg import render_svg
from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.models import GraphScan, StoredEntity


def _scan() -> GraphScan:
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    return GraphScan(
        entities=[
            StoredEntity(
                id="person:alice",
                type="Person",
                canonical_name="Alice",
                acl_ref="acl:finance",
            ),
            StoredEntity(
                id="company:acme-1",
                type="Company",
                canonical_name="Acme",
            ),
            StoredEntity(
                id="company:acme-2",
                type="Company",
                canonical_name="Acme Holdings",
            ),
        ],
        facts=[
            Fact(
                id=UUID("00000000-0000-0000-0000-000000000123"),
                subject_id="person:alice",
                predicate="works_at",
                object_id="company:acme-1",
                provenance=Provenance(
                    source_id="doc:1",
                    extractor_id="extractor:test",
                    extractor_version="1.2.3",
                    ontology_id="ontos.starter",
                    ontology_version="0.4",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=0.97,
                ),
                t_valid=when,
                ingested_at=when,
                acl_ref="acl:finance",
            )
        ],
        merges=[
            MergeRecord(
                canonical_id="company:acme-1",
                merged_ids=["company:acme-2"],
                resolver_id="exact-match",
                resolver_version="1.0",
                resolved_at=datetime(2026, 1, 3, tzinfo=UTC),
                reason="same registration number",
            )
        ],
    )


def test_render_svg_contains_raw_nodes_and_labels() -> None:
    svg = render_svg(_scan())

    assert svg.startswith("<svg")
    assert 'data-id="person:alice"' in svg
    assert 'data-id="company:acme-1"' in svg
    assert ">Alice<" in svg
    assert ">Acme<" in svg


def test_render_svg_contains_fact_metadata() -> None:
    svg = render_svg(_scan())

    assert 'data-kind="fact"' in svg
    assert 'data-predicate="works_at"' in svg
    assert 'data-source-id="doc:1"' in svg
    assert 'data-ontology-id="ontos.starter"' in svg
    assert 'data-ontology-version="0.4"' in svg
    assert 'data-acl-ref="acl:finance"' in svg
    assert ">works_at<" in svg


def test_render_svg_contains_merge_relationship() -> None:
    svg = render_svg(_scan())

    assert 'data-kind="merge"' in svg
    assert 'data-predicate="MERGED_WITH"' in svg
    assert 'data-resolver-id="exact-match"' in svg
    assert ">MERGED_WITH<" in svg


def test_render_svg_is_deterministic() -> None:
    scan = _scan()
    reversed_scan = GraphScan(
        entities=list(reversed(scan.entities)),
        facts=list(reversed(scan.facts)),
        merges=list(reversed(scan.merges)),
    )

    assert render_svg(scan) == render_svg(reversed_scan)


def test_same_column_arcs_always_fan_outward() -> None:
    """#123 review: parallel same-column arcs must not fold back into the column."""
    from ontos.exporters.svg import _NODE_WIDTH, _curve

    subject, target = (40, 40), (40, 200)
    column_edge = subject[0] + _NODE_WIDTH
    reaches = []
    for lane in (0, 1, -1, 2, -2, 3, -3):
        path, _ = _curve(subject, target, same_column=True, lane=lane)
        control_x = float(path.split("C")[1].split(",")[0].split()[0])
        reaches.append(control_x - column_edge)

    assert all(reach > 0 for reach in reaches)
    assert reaches == sorted(reaches) and len(set(reaches)) == len(reaches)
