"""Fail-closed, ACL-scoped source selection for operator re-ingestion."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from ontos.ingest.base import SourceDocument
from ontos.storage.sources import SourceRecord


class ScopedReingestError(ValueError):
    """Requested re-ingestion scope could not be safely established."""


def select_scoped_sources(
    *,
    requested_sources: Sequence[SourceRecord],
    available_documents: Sequence[SourceDocument],
    recorded_sources: Collection[SourceRecord],
) -> list[SourceDocument]:
    """Select exact registered source/ACL pairs before any ingestion writes.

    recorded_sources must come from trusted storage, not operator input.
    The caller must independently authorize the operator and source ACLs.
    The result is not authorization to read an otherwise forbidden source.
    """
    requested = list(requested_sources)

    if not requested or len(set(requested)) != len(requested):
        raise ScopedReingestError("Invalid scoped re-ingestion request")

    for record in requested:
        if not record.source_id.strip() or (
            record.acl_ref is not None and not record.acl_ref.strip()
        ):
            raise ScopedReingestError("Invalid scoped re-ingestion request")

    available: dict[SourceRecord, SourceDocument] = {}

    for document in available_documents:
        identity = SourceRecord(document.source_id, document.acl_ref)
        if (
            not identity.source_id.strip()
            or (identity.acl_ref is not None and not identity.acl_ref.strip())
            or identity in available
        ):
            raise ScopedReingestError("Scoped re-ingestion preflight failed")
        available[identity] = document

    trusted = set(recorded_sources)

    selected: list[SourceDocument] = []
    for identity in requested:
        if identity not in trusted or identity not in available:
            raise ScopedReingestError("Scoped re-ingestion preflight failed")
        selected.append(available[identity])

    return selected
