"""Sign and verify ontology migration approval records.

Runtime verification uses a purpose-specific HMAC over each complete
migration record, without audit database lookups or chain scans.
Article-12 approval events remain retention-governed audit evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence

from ontos.audit.base import AuditSink
from ontos.migration.records import MigrationRecord


class MigrationApprovalAuditError(ValueError):
    """A migration has no verifiable, matching approval."""


_MIGRATION_SIGNATURE_LABEL = b"ontology-migration-approval"


def _expected_migration_signature(
    record: MigrationRecord,
    key: bytes,
) -> str:
    """HMAC the canonical record, excluding only its signature."""
    payload = json.dumps(
        record.model_dump(mode="json", exclude={"approval_signature"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_migration_record(
    *,
    record: MigrationRecord,
    audit: AuditSink,
) -> MigrationRecord:
    """Sign an approved migration with a purpose-specific audit subkey."""
    if record.approval_query_id.int == 0 or len(record.approved_changes) != 1:
        raise MigrationApprovalAuditError(
            "Cannot sign a migration without exactly one approved change"
        )

    signature = _expected_migration_signature(
        record,
        audit.derive_key(_MIGRATION_SIGNATURE_LABEL),
    )
    return record.model_copy(update={"approval_signature": signature})


def verify_signed_migration_record(
    *,
    record: MigrationRecord,
    audit: AuditSink,
) -> None:
    """Verify migration integrity without reading the audit database."""
    if len(record.approved_changes) != 1:
        raise MigrationApprovalAuditError("Migration approval must authorize exactly one change")

    signature = record.approval_signature
    if not isinstance(signature, str):
        raise MigrationApprovalAuditError("Migration approval signature missing")

    try:
        expected = _expected_migration_signature(
            record,
            audit.derive_key(_MIGRATION_SIGNATURE_LABEL),
        )
    except Exception as exc:
        raise MigrationApprovalAuditError("Migration approval signing key unavailable") from exc

    if not hmac.compare_digest(signature, expected):
        raise MigrationApprovalAuditError("Migration approval signature verification failed")


async def verify_migration_approval_records(
    *,
    records: Sequence[MigrationRecord],
    audit: AuditSink,
) -> None:
    """Fail closed unless every migration has a valid approval signature.

    This verifies canonical migration YAML using a purpose-specific HMAC
    key. No audit database lookup or chain scan is performed. The original
    Article-12 approval events remain available for retention-governed
    audit inspection, but their deletion does not invalidate a migration.

    The signing key must remain available and unchanged for existing
    migrations to verify. Key rotation requires a controlled migration
    re-signing process.
    """
    for migration in records:
        verify_signed_migration_record(record=migration, audit=audit)
