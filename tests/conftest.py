"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


@pytest.fixture
def store() -> NetworkxStore:
    return NetworkxStore()


@pytest.fixture
def audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"test-signing-key-not-for-prod")


@pytest.fixture
def sample_fact() -> Fact:
    now = datetime.now(UTC)
    return Fact(
        id=uuid4(),
        subject_id="Acme Corp",
        predicate="acquired",
        object_id="Widget Inc",
        provenance=Provenance(
            source_id="sec-edgar:0001234-25-000001",
            extractor_id="llamaindex-pgi",
            extractor_version="0.11.0",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.98,
        ),
        t_valid=datetime(2025, 3, 14, tzinfo=UTC),
        t_invalid=None,
        ingested_at=now,
    )
