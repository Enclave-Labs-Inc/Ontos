"""Audited, fail-closed authorization for scoped re-ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Sequence

from ontos.audit.base import AuditSink, emit_record
from ontos.authz import ONTOLOGY_APPROVER, VIEW, AuthzBackend
from ontos.ingest.base import SourceDocument
from ontos.storage.sources import SourceRecord, SourceRegistry


class ScopedReingestDeniedError(PermissionError):
    """Re-ingestion authorization was not established."""


def _scope_reference(
    audit: AuditSink,
    sources: Sequence[SourceRecord],
) -> str:
    """Keyed reference; never put raw source IDs or ACLs in audit."""
    identities = sorted(
        ((source.source_id, source.acl_ref) for source in sources),
        key=lambda pair: (pair[0], pair[1] or ""),
    )
    canonical = json.dumps(
        identities,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(
        audit.derive_key(b"ontos.m3-3.reingest.scope.v1"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _audit_decision(
    *,
    audit: AuditSink,
    subject: str,
    sources: Sequence[SourceRecord],
    decision: str,
    recheck_interval_seconds: float = 0.0,
) -> None:
    arguments: dict[str, object] = {
        "identity_assurance": "operator_asserted",
        "requested_count": len(sources),
        "scope_ref": _scope_reference(audit, sources),
    }
    # Only recorded when per-write rechecks were relaxed, so the audit
    # shows the window in which a revocation could go unnoticed.
    if recheck_interval_seconds:
        arguments["recheck_interval_seconds"] = recheck_interval_seconds
    await emit_record(
        audit,
        query_id=None,
        agent_identity="ontos-cli-local-operator",
        acting_on_behalf_of=subject or None,
        query_text="scoped re-ingestion authorization",
        tool_invoked="ontos.reingest",
        tool_arguments=arguments,
        result_fact_ids=[],
        latency_ms=0.0,
        model_versions={"component": "scoped-reingest-m3-3-v1"},
        policy_decisions=[{"policy": "scoped_reingest", "result": decision}],
    )


async def authorize_scoped_reingest(
    *,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    sources: Sequence[SourceRecord],
    record_allow: bool = True,
) -> None:
    """Authorize one exact scope before opening source documents.

    The caller must initialize a persistent audit sink beforehand.
    This function must be re-run before ingestion to catch revoked
    permissions. Operator-asserted identity is not authentication.
    """
    requested = list(sources)

    if (
        not requested
        or len(set(requested)) != len(requested)
        or any(
            not source.source_id.strip()
            or (source.acl_ref is not None and not source.acl_ref.strip())
            for source in requested
        )
    ):
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="invalid_request",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion denied")

    if not subject.strip() or authz is None:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="deny",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion denied")

    try:
        has_role = await authz.check(
            subject,
            ONTOLOGY_APPROVER,
            "ontology",
        )

        if not has_role:
            await _audit_decision(
                audit=audit,
                subject=subject,
                sources=requested,
                decision="deny",
            )
            raise ScopedReingestDeniedError("Scoped re-ingestion denied")

        allowed = set(await authz.list_authorized_objects(subject, VIEW))

    except ScopedReingestDeniedError:
        raise
    except Exception:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="authorization_error",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion denied") from None

    if any(source.acl_ref is not None and source.acl_ref not in allowed for source in requested):
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="deny",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion denied")

    # Initial authorization remains explicitly audited.
    # Per-write rechecks can suppress repeated successful entries,
    # but every denial and authorization error is still audited.
    if record_allow:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="allow",
        )


async def prepare_scoped_reingest(
    *,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    sources: Sequence[SourceRecord],
    registry: SourceRegistry,
    load_source: Callable[[SourceRecord], Awaitable[SourceDocument]],
) -> list[SourceDocument]:
    """Authorize, verify, and load one explicitly scoped source at a time.

    The caller must initialize persistent audit before invoking this
    function. load_source must open only its supplied exact identity.

    This function performs no ingestion writes. Authorization must be
    rechecked again immediately before the eventual write operation.
    """

    from ontos.ingest.scoped import (
        ScopedReingestError,
        select_scoped_sources,
    )

    # First authorization happens before source-registry queries or reads.
    await authorize_scoped_reingest(
        authz=authz,
        audit=audit,
        subject=subject,
        sources=sources,
    )

    requested = list(sources)
    trusted: list[SourceRecord] = []

    try:
        # Verify the complete scope before opening any source file.
        for identity in requested:
            stored = await registry.get_source_record(
                identity.source_id,
                acl_ref=identity.acl_ref,
            )
            if stored != identity:
                raise ScopedReingestError("Source registration unavailable")
            trusted.append(stored)
    except Exception:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="scope_unavailable",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion unavailable") from None

    documents: list[SourceDocument] = []

    try:
        # Loading is limited to requested identities, not a full
        # directory scan or an unfiltered connector iteration.
        for identity in requested:
            document = await load_source(identity)
            documents.append(document)
    except Exception:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="source_unavailable",
        )
        raise ScopedReingestDeniedError("Scoped source loading failed") from None

    try:
        selected = select_scoped_sources(
            requested_sources=requested,
            available_documents=documents,
            recorded_sources=trusted,
        )

        # Reverify registrations after loading. Neither a removed
        # registration nor an ACL mismatch can pass preflight.
        for identity in requested:
            stored = await registry.get_source_record(
                identity.source_id,
                acl_ref=identity.acl_ref,
            )
            if stored != identity:
                raise ScopedReingestError("Source registration changed")

    except Exception:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="preflight_failed",
        )
        raise ScopedReingestDeniedError("Scoped re-ingestion preflight failed") from None

    # Revoked permissions prevent returning documents for ingestion.
    await authorize_scoped_reingest(
        authz=authz,
        audit=audit,
        subject=subject,
        sources=requested,
    )

    return selected
