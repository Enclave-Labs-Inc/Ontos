"""#123 review: detect a source re-ingested under a different ACL."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore


def _fact(source_id: str, acl_ref: str | None) -> Fact:
    return Fact(
        subject_id="Person:alice",
        predicate="works_at",
        object_id="Company:acme",
        provenance=Provenance(
            source_id=source_id,
            extractor_id="test",
            extractor_version="1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        acl_ref=acl_ref,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "attempted", "expected"),
    [
        (None, None, False),
        ("acl:hr", "acl:hr", False),
        (None, "acl:finance", True),
        ("acl:hr", None, True),
        ("acl:hr", "acl:finance", True),
    ],
)
async def test_networkx_detects_other_acl(
    stored: str | None, attempted: str | None, expected: bool
) -> None:
    store = NetworkxStore()
    await store.add_fact(_fact("doc.md", stored))

    assert await store.source_has_other_acl("doc.md", acl_ref=attempted) is expected
    assert await store.source_has_other_acl("other.md", acl_ref=attempted) is False
