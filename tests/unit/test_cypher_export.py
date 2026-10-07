"""#32: Cypher visualization exporter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from ontos.exporters.cypher import render_cypher, sanitize_identifier
from ontos.resolver.base import MergeRecord
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.models import GraphScan, StoredEntity


def _scan() -> GraphScan:
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    fact = Fact(
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
    return GraphScan(
        entities=[
            StoredEntity(
                id="person:alice",
                type="Person",
                canonical_name="Alice",
                aliases=["A. Smith"],
                properties={"team": "finance"},
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
        facts=[fact],
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


def test_sanitize_identifier_strips_unsafe_characters_and_uses_fallback() -> None:
    assert sanitize_identifier("Company-Type!", fallback="Entity") == "CompanyType"
    assert (
        sanitize_identifier("works-at!", fallback="RELATED_TO", uppercase=True)
        == "WORKSAT"
    )
    assert sanitize_identifier("!!!", fallback="Entity") == "Entity"


def test_render_cypher_uses_typed_nodes_and_idempotent_merge() -> None:
    cypher = render_cypher(_scan())

    assert "CREATE INDEX IF NOT EXISTS FOR (n:`Company`) ON (n.id);" in cypher
    assert "CREATE INDEX IF NOT EXISTS FOR (n:`Person`) ON (n.id);" in cypher
    assert "MERGE (n:`Person` {id: 'person:alice'})" in cypher
    assert "MERGE (n:`Company` {id: 'company:acme-1'})" in cypher
    assert "canonical_name: 'Alice'" in cypher
    assert "acl_ref: 'acl:finance'" in cypher


def test_render_cypher_exports_fact_and_merge_edges() -> None:
    cypher = render_cypher(_scan())

    expected_fact = (
        "[r:`WORKS_AT` "
        "{fact_id: '00000000-0000-0000-0000-000000000123'}]"
    )
    assert expected_fact in cypher
    assert "predicate: 'works_at'" in cypher
    assert "source_id: 'doc:1'" in cypher
    assert "ontology_id: 'ontos.starter'" in cypher
    assert "ontology_version: '0.4'" in cypher
    assert "confidence: 'EXTRACTED'" in cypher
    assert "acl_ref: 'acl:finance'" in cypher

    expected_merge = (
        "MERGE (m)-[r:MERGED_WITH "
        "{resolver_id: 'exact-match'}]->(c)"
    )
    assert expected_merge in cypher
    assert "resolver_version: '1.0'" in cypher
    assert "reason: 'same registration number'" in cypher


def test_render_cypher_is_deterministic() -> None:
    scan = _scan()
    reversed_scan = scan.model_copy(
        update={
            "entities": list(reversed(scan.entities)),
            "facts": list(reversed(scan.facts)),
            "merges": list(reversed(scan.merges)),
        }
    )

    assert render_cypher(scan) == render_cypher(reversed_scan)
