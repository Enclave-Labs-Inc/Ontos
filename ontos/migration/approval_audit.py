"""Verify that migration records reference genuine Article-12 approvals.

This module validates approval evidence only. Runtime integration must
invoke it before treating a configured migration registry as trusted.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ontos.audit.base import AuditSink, lookup_record
from ontos.migration.records import MigrationRecord
from ontos.runtime.candidate_inspection import candidate_audit_reference
from ontos.runtime.models import ArticleTwelveField

_CHANGE_REFERENCE_LABEL = b"ontology-approval-change-reference"


class MigrationApprovalAuditError(ValueError):
    """A migration has no verifiable, matching approval."""


@runtime_checkable
class _AsyncChainVerifier(Protocol):
    async def verify_chain_async(self) -> bool: ...


async def verify_migration_approval_records(
    *,
    records: Sequence[MigrationRecord],
    audit: AuditSink,
) -> None:
    """Fail closed unless every migration has a genuine matching approval.

    Verify the complete audit chain before trusting individual records.
    This function performs no ontology or migration registration.
    """
    if not records:
        return

    try:
        if isinstance(audit, _AsyncChainVerifier):
            chain_valid = await audit.verify_chain_async()
        else:
            chain_valid = audit.verify_chain()
    except Exception as exc:
        raise MigrationApprovalAuditError("Migration approval audit unavailable") from exc

    if not chain_valid:
        raise MigrationApprovalAuditError("Migration approval audit chain failed verification")

    for migration in records:
        if len(migration.approved_changes) != 1:
            raise MigrationApprovalAuditError(
                "Migration approval must authorize exactly one change"
            )

        try:
            event = await lookup_record(audit, migration.approval_query_id)
        except Exception as exc:
            raise MigrationApprovalAuditError(
                "Migration approval audit lookup unavailable"
            ) from exc

        if event is None or event.query_id != migration.approval_query_id:
            raise MigrationApprovalAuditError("Migration approval audit record not found")

        fields = event.article12

        try:
            arguments = json.loads(fields[ArticleTwelveField.TOOL_ARGUMENTS])
            decisions = json.loads(fields[ArticleTwelveField.POLICY_DECISIONS])
            evidence_refs = json.loads(fields[ArticleTwelveField.RESULT_FACT_IDS])
            versions = json.loads(fields[ArticleTwelveField.MODEL_VERSIONS])
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationApprovalAuditError("Invalid migration approval audit payload") from exc

        if (
            fields.get(ArticleTwelveField.TOOL_INVOKED) != "ontology_approve"
            or decisions
            != [
                {
                    "policy": "ontology_approval",
                    "result": "approval_granted",
                }
            ]
            or versions != {"component": "ontology-approval-m3-2-v1"}
            or not isinstance(arguments, dict)
            or not isinstance(evidence_refs, list)
            or not evidence_refs
        ):
            raise MigrationApprovalAuditError("Referenced event is not a valid ontology approval")

        change = migration.approved_changes[0]
        canonical_change = json.dumps(
            change.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_ref = candidate_audit_reference(
            audit.derive_key(_CHANGE_REFERENCE_LABEL),
            canonical_change,
        )

        stored_ref = arguments.get("change_ref")

        if (
            arguments.get("ontology_id") != migration.ontology_id
            or arguments.get("base_version") != migration.from_version
            or arguments.get("new_version") != migration.to_version
            or arguments.get("change_kind") != change.action
            or arguments.get("identity_assurance") != "operator_asserted"
            or not isinstance(stored_ref, str)
            or not hmac.compare_digest(stored_ref, expected_ref)
            or type(arguments.get("support_count")) is not int
            or arguments["support_count"] < 1
            or arguments["support_count"] != len(evidence_refs)
            or not all(
                isinstance(ref, str)
                and len(ref) == 64
                and all(char in "0123456789abcdef" for char in ref)
                for ref in evidence_refs
            )
        ):
            raise MigrationApprovalAuditError(
                "Migration approval does not match recorded authorization"
            )
