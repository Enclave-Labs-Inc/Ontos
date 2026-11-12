"""M3-3 operator-triggered scoped re-ingestion execution.

This module composes existing authorization, preflight, and ingestion
components. It does not authenticate operator-asserted identities;
the deployment must establish trusted operator identity externally.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Sequence

from ontos.audit.base import AuditSink
from ontos.authz import AuthzBackend
from ontos.extraction import Extractor
from ontos.ingest.base import SourceDocument
from ontos.ingest.scoped_connector import PreparedScopedConnector
from ontos.ontology import Ontology
from ontos.pipeline.ingest import (
    ErrorPolicy,
    IngestPipeline,
    IngestReport,
)
from ontos.resolver import Resolver
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    _audit_decision,
    authorize_scoped_reingest,
    prepare_scoped_reingest,
)
from ontos.storage.base import CandidateStore, GraphStore
from ontos.storage.sources import SourceRecord, SourceRegistry

# Upper bound on how long a revoked permission can go unnoticed.
MAX_RECHECK_INTERVAL_SECONDS = 300.0


def validate_recheck_interval(seconds: float) -> float:
    """Reject negative, non-finite, or over-long recheck intervals."""
    if (
        isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or not 0 <= seconds <= MAX_RECHECK_INTERVAL_SECONDS
    ):
        raise ValueError(
            f"recheck interval must be between 0 and {MAX_RECHECK_INTERVAL_SECONDS:g} seconds"
        )
    return float(seconds)


async def run_scoped_reingest(
    *,
    authz: AuthzBackend | None,
    audit: AuditSink,
    subject: str,
    sources: Sequence[SourceRecord],
    registry: SourceRegistry,
    load_source: Callable[[SourceRecord], Awaitable[SourceDocument]],
    extractor: Extractor,
    resolver: Resolver | None,
    store: GraphStore,
    ontology: Ontology,
    candidate_store: CandidateStore | None = None,
    emit_completion: bool = True,
    recheck_interval_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> IngestReport:
    """Execute explicitly scoped re-ingestion with repeated ACL checks.

    The caller must initialize a persistent audit sink before invocation.
    Source loading occurs only after authorization and trusted registration
    checks. Each graph mutation is independently permission-gated.

    Partial writes are possible if permissions are revoked mid-operation.

    With the default `recheck_interval_seconds=0` every mutation is
    rechecked. A positive interval reuses the last successful recheck
    for up to that many seconds, so a revocation can go unnoticed for
    at most that long; denials and errors are never reused.
    """
    requested = tuple(sources)
    try:
        interval = validate_recheck_interval(recheck_interval_seconds)
    except ValueError:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            decision="invalid_request",
        )
        raise

    prepared = await prepare_scoped_reingest(
        authz=authz,
        audit=audit,
        subject=subject,
        sources=requested,
        registry=registry,
        load_source=load_source,
    )

    connector = PreparedScopedConnector(prepared)
    requested_set = set(requested)
    last_allowed_at: float | None = None

    async def before_write(identity: SourceRecord) -> None:
        nonlocal last_allowed_at

        # Scope membership is checked on every write, never reused.
        if identity not in requested_set:
            await _audit_decision(
                audit=audit,
                subject=subject,
                sources=requested,
                recheck_interval_seconds=interval,
                decision="deny",
            )
            raise ScopedReingestDeniedError("Scoped re-ingestion denied")

        if interval > 0 and last_allowed_at is not None and clock() - last_allowed_at < interval:
            return
        last_allowed_at = None

        # Recheck the complete original scope rather than only the
        # current document, to prevent partial-scope substitution.
        await authorize_scoped_reingest(
            authz=authz,
            audit=audit,
            subject=subject,
            sources=requested,
            record_allow=False,
        )

        try:
            stored = await registry.get_source_record(
                identity.source_id,
                acl_ref=identity.acl_ref,
            )
        except Exception:
            # A failed registry lookup cannot establish the scope.
            await _audit_decision(
                audit=audit,
                subject=subject,
                sources=requested,
                recheck_interval_seconds=interval,
                decision="scope_unavailable",
            )
            raise ScopedReingestDeniedError("Scoped re-ingestion denied") from None

        if stored != identity:
            # A completed lookup with a changed or missing registration
            # is an explicit scope denial, not a registry outage.
            await _audit_decision(
                audit=audit,
                subject=subject,
                sources=requested,
                recheck_interval_seconds=interval,
                decision="deny",
            )
            raise ScopedReingestDeniedError("Scoped re-ingestion denied")

        last_allowed_at = clock()

    pipeline = IngestPipeline(
        connector=connector,
        extractor=extractor,
        resolver=resolver,
        store=store,
        ontology=ontology,
        candidate_store=candidate_store,
        source_registry=registry,
        error_policy=ErrorPolicy.FAIL_FAST,
        before_write=before_write,
    )

    try:
        report = await pipeline.run()
    except Exception:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            recheck_interval_seconds=interval,
            decision="failed",
        )
        raise

    # The CLI defers completion until the store has persisted.
    # Direct callers retain the existing completion behavior.
    if emit_completion:
        await _audit_decision(
            audit=audit,
            subject=subject,
            sources=requested,
            recheck_interval_seconds=interval,
            decision="completed",
        )
    return report
