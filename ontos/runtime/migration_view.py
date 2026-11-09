"""Verified ontology migrations for reads that bypass the executor (#112).

M3-1 applied the strict registry inside `DeterministicExecutor` only, so a
fact without a verified migration path was hidden from `ask` and `ontos
query` but still returned by the MCP `search` / `traverse` / `explain` /
`provenance` tools and by `ontos export`. Every read surface now projects
facts through the same registry and active ontology.

Projection runs after ACL filtering, never rewrites stored facts, and does
not report what it excluded to the caller: no counts, ids or warnings in
responses. Exclusions are logged for the operator instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import structlog

from ontos.audit.base import AuditSink
from ontos.executor.deterministic import hop_distances
from ontos.migration.approval_audit import (
    MigrationApprovalAuditError,
    verify_migration_approval_records,
)
from ontos.migration.loading import (
    MigrationConfigurationError,
    load_unverified_migration_registry_with_records,
)
from ontos.migration.records import MigrationRecord
from ontos.migration.registry import MigrationRegistry
from ontos.ontology import Ontology, load_ontology
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import Fact
from ontos.storage.models import GraphScan

log = structlog.get_logger()


@dataclass
class _ApprovalVerificationGate:
    """Verification state for the pinned migration records."""

    records: tuple[MigrationRecord, ...]
    verified: bool = False


@dataclass(frozen=True)
class MigrationView:
    """A migration projection requiring approval verification."""

    ontology: Ontology
    registry: MigrationRegistry
    verification_gate: _ApprovalVerificationGate | None = None

    def require_verified_approvals(self) -> None:
        """Refuse migration-backed reads before signature verification."""
        gate = self.verification_gate
        if gate is not None and not gate.verified:
            raise MigrationApprovalAuditError("Migration approval audit has not been verified")

    async def verify_approvals(self, audit: AuditSink) -> None:
        """Verify the records used to construct this registry.

        This is a startup/invocation signature-verification gate,
        not continuous monitoring. A long-running server does not re-read
        migration YAML or reverify signatures after startup. Restart the
        runtime after migration or signing-key changes.

        Audit retention pruning does not invalidate signed migrations.
        """
        gate = self.verification_gate
        if gate is None:
            return

        await verify_migration_approval_records(
            records=gate.records,
            audit=audit,
        )
        gate.verified = True

    def audit_decision(self) -> dict[str, str]:
        """Policy decision naming the ontology results were projected onto."""
        return {
            "policy": "ontology_migration",
            "ontology_id": self.ontology.id,
            "ontology_version": self.ontology.version,
        }

    async def project(self, facts: Iterable[Fact]) -> list[Fact]:
        """Facts translated to the active ontology; unverifiable ones dropped."""
        self.require_verified_approvals()
        projected: list[Fact] = []
        for fact in facts:
            migrated, warnings = await self.registry.migrate_fact(fact, self.ontology)
            for warning in warnings:
                log.info(
                    "ontology-migration-excluded" if migrated is None else "ontology-migration",
                    fact_id=str(warning.fact_id),
                    from_version=warning.from_version,
                    to_version=warning.to_version,
                    migration_id=warning.migration_id,
                    reason=warning.reason,
                )
            if migrated is not None:
                projected.append(migrated)
        return projected

    async def project_reachable(
        self,
        start: str,
        facts: Iterable[Fact],
        direction: Literal["out", "in", "both"],
    ) -> list[Fact]:
        """Project a traversal, keeping only facts still reachable from `start`.

        An excluded fact must not bridge to the facts beyond it.
        """
        projected = await self.project(facts)
        distances = hop_distances(start, projected, direction)
        return [fact for fact in projected if fact.id in distances]

    async def project_scan(self, scan: GraphScan) -> GraphScan:
        """Project an export snapshot.

        Untyped endpoint nodes stay only while a kept fact or a merge record
        names them (#100), so an excluded fact's endpoints don't linger.
        """
        facts = await self.project(scan.facts)
        named = {fact.subject_id for fact in facts} | {fact.object_id for fact in facts}
        for record in scan.merges:
            named.add(record.canonical_id)
            named.update(record.merged_ids)
        entities = [
            entity for entity in scan.entities if entity.type is not None or entity.id in named
        ]
        return scan.model_copy(update={"facts": facts, "entities": entities})


def build_migration_view(
    config: RuntimeConfig,
    *,
    active_ontology: Ontology | None = None,
) -> MigrationView | None:
    """Load and verify the configured migrations, or None when not configured.

    An explicitly provided ontology takes precedence over `ONTOS_ONTOLOGY_PATH`.
    """
    migration_dir = config.migration_dir
    history_dir = config.ontology_history_dir

    if migration_dir is None and history_dir is None:
        return None

    if migration_dir is None or history_dir is None:
        raise MigrationConfigurationError(
            "both migration and ontology-history directories are required"
        )

    if active_ontology is None:
        if config.ontology_path is None:
            raise MigrationConfigurationError(
                "an active ontology is required when migrations are configured"
            )
        active_ontology = load_ontology(config.ontology_path)

    registry, records = load_unverified_migration_registry_with_records(
        migration_dir,
        active_ontology=active_ontology,
        historical_ontology_dir=history_dir,
        strict=True,
    )
    return MigrationView(
        ontology=active_ontology,
        registry=registry,
        verification_gate=_ApprovalVerificationGate(
            records=tuple(records),
        ),
    )
