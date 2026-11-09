"""Recompute a selected ontology change from authorized candidate evidence.

Pure computation only. The caller MUST enforce ontology_approver,
source VIEW permissions, audit-before-read, and final authorization
before using this result to publish versioned artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ontos.migration.records import (
    AddPatternChange,
    MapToExistingChange,
    NewRelationTypeChange,
)
from ontos.ontology.loader import Ontology
from ontos.ontology.suggestion_materialization import (
    materialize_supported_suggestions,
)
from ontos.ontology.suggestion_proposals import ValidatedSemanticProposal
from ontos.ontology.suggestion_support import resolve_proposal_support
from ontos.ontology.suggestion_validation import filter_ontology_suggestions
from ontos.ontology.suggestions import suggest_missing_patterns
from ontos.storage.base import StoredCandidate

ApprovalChange = AddPatternChange | MapToExistingChange | NewRelationTypeChange


class ApprovalSuggestionNotFoundError(ValueError):
    """The selected change has no matching, eligible supporting suggestion."""


@dataclass(frozen=True)
class RecomputedApproval:
    change: ApprovalChange
    acl_ref: str | None
    supporting_candidate_refs: tuple[str, ...]
    support_count: int


def recompute_approval_change(
    *,
    authorized_candidates: list[StoredCandidate],
    ontology: Ontology,
    requested_change: ApprovalChange,
    acl_ref: str | None,
    reference_key: bytes,
) -> RecomputedApproval:
    """Rebuild one change from a single authorized ACL evidence scope.

    Never accept candidate references or support counts from the operator.
    An empty or hidden suggestion produces the same exception.
    """
    if not reference_key:
        raise ValueError("A derived candidate-reference key is required")

    scoped = [record for record in authorized_candidates if record.candidate.acl_ref == acl_ref]

    if isinstance(requested_change, AddPatternChange):
        suggestions = suggest_missing_patterns(
            candidates=scoped,
            ontology=ontology,
            reference_key=reference_key,
        )

        # Match the final validation used by the suggestions service.
        suggestions, _ = filter_ontology_suggestions(
            list(suggestions),
            ontology,
            require_m2b_evidence=True,
        )

        matching = [
            suggestion
            for suggestion in suggestions
            if suggestion.kind == "add_pattern"
            and suggestion.pattern is not None
            and (
                suggestion.pattern.subject_type,
                suggestion.pattern.predicate,
                suggestion.pattern.object_type,
            )
            == (
                requested_change.subject_type,
                requested_change.predicate,
                requested_change.object_type,
            )
        ]

        if len(matching) != 1:
            raise ApprovalSuggestionNotFoundError("No such suggestion")

        suggestion = matching[0]
        assert suggestion.pattern is not None

        change: ApprovalChange = AddPatternChange(
            action="add_pattern",
            subject_type=suggestion.pattern.subject_type,
            predicate=suggestion.pattern.predicate,
            object_type=suggestion.pattern.object_type,
        )

        return RecomputedApproval(
            change=change,
            acl_ref=acl_ref,
            supporting_candidate_refs=tuple(suggestion.supporting_candidate_refs),
            support_count=suggestion.support_count,
        )

    if isinstance(requested_change, MapToExistingChange):
        observed = requested_change.observed_predicate
        subject = requested_change.subject_type
        object_type = requested_change.object_type
        kind: Literal["map_to_existing", "new_relation_type"] = "map_to_existing"
        target = requested_change.maps_to
    else:
        observed = requested_change.relation_type
        subject = requested_change.subject_type
        object_type = requested_change.object_type
        kind = "new_relation_type"
        target = requested_change.relation_type

    proposal = ValidatedSemanticProposal(
        acl_ref=acl_ref,
        evidence_pattern=(subject, observed, object_type),
        kind=kind,
        relation_type=target,
    )

    support = resolve_proposal_support(
        proposals=[proposal],
        authorized_candidates=scoped,
        ontology=ontology,
        reference_key=reference_key,
    )

    if len(support.proposals) != 1:
        raise ApprovalSuggestionNotFoundError("No such suggestion")

    materialized = materialize_supported_suggestions(
        supports=support.proposals,
        ontology=ontology,
        generator_id="ontos.approval.recomputed",
        generator_version="m3-2-v1",
    )

    # Materialization alone does not run the shared final validation.
    # Reject incomplete review evidence or ontology-inconsistent results.
    accepted, _ = filter_ontology_suggestions(
        list(materialized.suggestions),
        ontology,
        require_m2b_evidence=True,
    )

    if len(accepted) != 1:
        raise ApprovalSuggestionNotFoundError("No such suggestion")

    suggestion = accepted[0]
    assert suggestion.pattern is not None

    if isinstance(requested_change, MapToExistingChange):
        if suggestion.kind != "map_to_existing":
            raise ApprovalSuggestionNotFoundError("No such suggestion")

        change = MapToExistingChange(
            action="map_to_existing",
            subject_type=suggestion.pattern.subject_type,
            observed_predicate=observed,
            object_type=suggestion.pattern.object_type,
            maps_to=suggestion.pattern.predicate,
        )
    else:
        if suggestion.kind != "new_relation_type":
            raise ApprovalSuggestionNotFoundError("No such suggestion")

        change = NewRelationTypeChange(
            action="new_relation_type",
            subject_type=suggestion.pattern.subject_type,
            relation_type=suggestion.pattern.predicate,
            object_type=suggestion.pattern.object_type,
            description=requested_change.description,
            cardinality=requested_change.cardinality,
        )

    return RecomputedApproval(
        change=change,
        acl_ref=acl_ref,
        supporting_candidate_refs=tuple(suggestion.supporting_candidate_refs),
        support_count=suggestion.support_count,
    )
