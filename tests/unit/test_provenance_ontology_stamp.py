"""#34 — Provenance ontology-stamp tests.

Pins the back-compat contract: `ontology_id` / `ontology_version`
default to `""` so the 40 existing test-fixture call sites keep
constructing Provenance with the pre-#34 argument shape.

The empty sentinel is also what pre-#34 Fact pickles unpickle as:
Pydantic treats a missing field with a default as valid, so the
NetworkxStore V2 pickle format tolerates missing ontology fields
without a magic-header bump.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ontos.runtime.models import Confidence, Provenance


def _make_prov(**overrides: object) -> Provenance:
    defaults: dict[str, object] = {
        "source_id": "doc1",
        "extractor_id": "llm.v1",
        "extractor_version": "1.0",
        "confidence": Confidence.EXTRACTED,
        "confidence_score": 0.9,
    }
    defaults.update(overrides)
    return Provenance(**defaults)  # type: ignore[arg-type]


def test_ontology_fields_default_to_empty_string_for_back_compat() -> None:
    prov = _make_prov()
    assert prov.ontology_id == ""
    assert prov.ontology_version == ""


def test_ontology_fields_round_trip_when_provided() -> None:
    prov = _make_prov(ontology_id="ontos.starter", ontology_version="0.2")
    assert prov.ontology_id == "ontos.starter"
    assert prov.ontology_version == "0.2"


def test_provenance_remains_frozen_after_adding_ontology_fields() -> None:
    prov = _make_prov(ontology_id="x", ontology_version="1")
    with pytest.raises(ValidationError):
        prov.ontology_id = "mutated"  # type: ignore[misc]


def test_legacy_provenance_dict_still_validates() -> None:
    # Simulates a pre-#34 pickle: dict shape has no ontology_* keys.
    # Pydantic fills defaults; validation succeeds.
    legacy = {
        "source_id": "doc1",
        "extractor_id": "llm.v1",
        "extractor_version": "1.0",
        "confidence": "EXTRACTED",
        "confidence_score": 0.9,
    }
    prov = Provenance.model_validate(legacy)
    assert prov.ontology_id == ""
    assert prov.ontology_version == ""


def test_stamp_survives_model_copy() -> None:
    # fact.model_copy(update=...) is the migration workhorse; verify
    # the stamp doesn't get silently reset when a migration touches
    # some other field on the Fact or its Provenance.
    original = _make_prov(ontology_id="ontos.starter", ontology_version="0.1")
    updated = original.model_copy(update={"confidence_score": 1.0})
    assert updated.ontology_id == "ontos.starter"
    assert updated.ontology_version == "0.1"
