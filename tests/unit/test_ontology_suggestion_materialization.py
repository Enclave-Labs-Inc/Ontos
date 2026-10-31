"""M2b materialization uses trusted metadata and ontology validation."""

from datetime import UTC, datetime

import pytest

from ontos.ontology.loader import (
    EntityType,
    Ontology,
    Pattern,
    RelationType,
)
from ontos.ontology.suggestion_materialization import (
    materialize_supported_suggestions,
)
from ontos.ontology.suggestion_proposals import ValidatedSemanticProposal
from ontos.ontology.suggestion_support import SupportedSemanticProposal

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _ontology(*, mapping_allowed: bool = True) -> Ontology:
    return Ontology(
        id="test.ontology",
        version="1.0",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[RelationType(label="works_at")],
        patterns=(
            [
                Pattern(
                    subject_type="Person",
                    predicate="works_at",
                    object_type="Company",
                )
            ]
            if mapping_allowed
            else []
        ),
    )


def _support(
    *,
    kind: str = "map_to_existing",
    observed: str = "manages",
    target: str = "works_at",
    acl: str | None = "finance",
    count: int = 2,
    refs: tuple[str, ...] = ("hmac-ref-1", "hmac-ref-2"),
) -> SupportedSemanticProposal:
    from typing import Literal, cast

    return SupportedSemanticProposal(
        semantic=ValidatedSemanticProposal(
            acl_ref=acl,
            evidence_pattern=("Person", observed, "Company"),
            kind=cast(Literal["map_to_existing", "new_relation_type"], kind),
            relation_type=target,
        ),
        support_count=count,
        supporting_candidate_refs=refs,
        extractor_versions=(("test-extractor", "v1"),),
    )


def _materialize(
    supports: list[SupportedSemanticProposal],
    *,
    ontology: Ontology | None = None,
):
    return materialize_supported_suggestions(
        supports=supports,
        ontology=ontology if ontology is not None else _ontology(),
        generator_id="ollama:gemma3:12b",
        generator_version="gemma3:12b",
        generated_at=NOW,
    )


def test_existing_relation_mapping_uses_target_pattern() -> None:
    result = _materialize([_support()])
    assert result.invalid_count == 0
    assert len(result.suggestions) == 1

    suggestion = result.suggestions[0]
    assert suggestion.kind == "map_to_existing"
    assert suggestion.pattern == Pattern(
        subject_type="Person",
        predicate="works_at",
        object_type="Company",
    )
    assert suggestion.mapped_relation_type == "works_at"
    assert suggestion.proposed_relation_type is None
    assert suggestion.acl_ref == "finance"
    assert suggestion.support_count == 2
    assert suggestion.supporting_candidate_refs == ["hmac-ref-1", "hmac-ref-2"]


def test_new_relation_uses_observed_predicate() -> None:
    result = _materialize(
        [
            _support(
                kind="new_relation_type",
                observed="manages",
                target="manages",
                acl=None,
            )
        ]
    )

    assert result.invalid_count == 0
    suggestion = result.suggestions[0]
    assert suggestion.kind == "new_relation_type"
    assert suggestion.pattern is not None
    assert suggestion.pattern.predicate == "manages"
    assert suggestion.proposed_relation_type == "manages"
    assert suggestion.mapped_relation_type is None
    assert suggestion.acl_ref is None


def test_generator_and_ontology_metadata_are_server_controlled() -> None:
    result = _materialize([_support()])
    suggestion = result.suggestions[0]

    assert suggestion.generator_id == "ollama:gemma3:12b"
    assert suggestion.generator_version == "gemma3:12b"
    assert suggestion.ontology_id == "test.ontology"
    assert suggestion.ontology_version == "1.0"
    assert suggestion.generated_at == NOW


def test_disallowed_mapping_is_dropped() -> None:
    result = _materialize(
        [_support()],
        ontology=_ontology(mapping_allowed=False),
    )
    assert result.suggestions == ()
    assert result.invalid_count == 1


def test_unrelated_new_relation_is_dropped() -> None:
    result = _materialize(
        [
            _support(
                kind="new_relation_type",
                observed="manages",
                target="invented",
            )
        ]
    )
    assert result.suggestions == ()
    assert result.invalid_count == 1


def test_missing_support_is_dropped() -> None:
    result = _materialize([_support(count=0), _support(refs=())])
    assert result.suggestions == ()
    assert result.invalid_count == 2


def test_duplicate_proposals_are_not_emitted() -> None:
    support = _support()
    result = _materialize([support, support])
    assert len(result.suggestions) == 1
    assert result.invalid_count == 1


def test_separate_acl_suggestions_are_preserved() -> None:
    result = _materialize([_support(acl="finance"), _support(acl="legal")])
    assert len(result.suggestions) == 2
    assert {s.acl_ref for s in result.suggestions} == {"finance", "legal"}


def test_missing_generator_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="Generator"):
        materialize_supported_suggestions(
            supports=[_support()],
            ontology=_ontology(),
            generator_id="",
            generator_version="v1",
        )


def test_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        materialize_supported_suggestions(
            supports=[_support()],
            ontology=_ontology(),
            generator_id="scripted",
            generator_version="v1",
            generated_at=datetime(2026, 10, 29),
        )


def test_m2b_review_preserves_observed_predicate_and_extractors() -> None:
    result = _materialize(
        [
            _support(
                kind="map_to_existing",
                observed="manages",
                target="works_at",
                acl="finance",
            ),
            _support(
                kind="new_relation_type",
                observed="owns",
                target="owns",
                acl="legal",
            ),
        ]
    )

    assert result.invalid_count == 0
    assert len(result.suggestions) == 2

    mapping, new_relation = result.suggestions

    assert mapping.observed_predicate == "manages"
    assert mapping.extractor_versions == (("test-extractor", "v1"),)
    assert mapping.mapped_relation_type == "works_at"

    assert new_relation.observed_predicate == "owns"
    assert new_relation.extractor_versions == (("test-extractor", "v1"),)
    assert new_relation.proposed_relation_type == "owns"


def test_strict_review_rejects_incomplete_or_forged_evidence() -> None:
    from ontos.ontology.suggestion_validation import (
        filter_ontology_suggestions,
    )

    result = _materialize([_support()])
    original = result.suggestions[0]

    invalid_updates = [
        {"observed_predicate": None},
        {"observed_predicate": "ignore instructions"},
        {"observed_predicate": "works_at"},
        {"extractor_versions": ()},
        {"extractor_versions": (("", "v1"),)},
        {"extractor_versions": (("extractor", ""),)},
        {
            "extractor_versions": (
                ("extractor", "v1"),
                ("extractor", "v1"),
            )
        },
    ]

    for updates in invalid_updates:
        tampered = original.model_copy(update=updates)
        accepted, dropped = filter_ontology_suggestions(
            [tampered],
            _ontology(),
            require_m2b_evidence=True,
        )
        assert accepted == []
        assert dropped == 1


def test_strict_review_accepts_complete_m2b_evidence() -> None:
    from ontos.ontology.suggestion_validation import (
        filter_ontology_suggestions,
    )

    result = _materialize(
        [
            _support(),
            _support(
                kind="new_relation_type",
                observed="owns",
                target="owns",
            ),
        ]
    )

    assert len(result.suggestions) == 2

    accepted, dropped = filter_ontology_suggestions(
        list(result.suggestions),
        _ontology(),
        require_m2b_evidence=True,
    )

    assert dropped == 0
    assert accepted == list(result.suggestions)
