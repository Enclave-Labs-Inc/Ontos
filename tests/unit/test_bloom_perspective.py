"""#32: Bloom Perspective asset regression tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PERSPECTIVE_PATH = ROOT / "scripts" / "bloom-perspective.json"
ONTOLOGY_PATHS = [
    ROOT / "docs" / "ontology" / "examples" / "starter.yaml",
    ROOT / "docs" / "ontology" / "examples" / "fintech.yaml",
    ROOT / "docs" / "ontology" / "examples" / "pharma.yaml",
]


def _perspective() -> dict[str, object]:
    return json.loads(PERSPECTIVE_PATH.read_text(encoding="utf-8"))


def _ontology_labels(section: str) -> set[str]:
    labels: set[str] = set()
    for path in ONTOLOGY_PATHS:
        ontology = yaml.safe_load(path.read_text(encoding="utf-8"))
        labels.update(item["label"] for item in ontology.get(section, []))
    return labels


def test_bloom_perspective_covers_example_entity_types() -> None:
    perspective = _perspective()
    categories = perspective["categories"]
    labels = perspective["labels"]

    expected = _ontology_labels("entity_types")

    assert {category["name"] for category in categories} == expected
    assert set(labels) == expected

    for category in categories:
        assert category["labels"] == [category["name"]]
        assert category["color"]
        assert category["size"] > 0
        assert category["captionKeys"] == [
            "0_CATEGORY_NAME_CAPTION_KEY",
            "canonical_name",
            "id",
        ]

        properties = {item["name"] for item in category["properties"]}
        assert {"id", "canonical_name", "entity_type", "acl_ref"} <= properties


def test_bloom_perspective_relationships_and_saved_views() -> None:
    perspective = _perspective()
    relationships = {
        relationship["id"]: relationship for relationship in perspective["relationshipTypes"]
    }

    ontology_relationships = {
        re.sub(r"[^A-Za-z0-9_]+", "", label).upper() for label in _ontology_labels("relation_types")
    }

    assert ontology_relationships <= set(relationships)
    assert "MERGED_WITH" in relationships

    for relationship_id, relationship in relationships.items():
        assert relationship["color"]
        assert relationship["size"] > 0

        if relationship_id == "MERGED_WITH":
            assert "resolver_id" in relationship["captionKeys"]
        else:
            assert "predicate" in relationship["captionKeys"]

    templates = {item["name"]: item["cypher"] for item in perspective["templates"]}

    assert "r.acl_ref IS NULL" in templates["Public facts"]
    assert "r.acl_ref IS NOT NULL" in templates["ACL-scoped facts"]
    assert "r.t_invalid IS NULL" in templates["Active facts"]
    assert "r.t_invalid IS NOT NULL" in templates["Closed historical facts"]
