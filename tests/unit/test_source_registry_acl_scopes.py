"""M3-3 regression: source identities are scoped by ACL."""

from __future__ import annotations

import pytest

from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord


@pytest.mark.asyncio
async def test_same_source_id_can_exist_under_two_restricted_acls() -> None:
    store = NetworkxStore()
    hr = SourceRecord("shared.txt", "acl:hr")
    finance = SourceRecord("shared.txt", "acl:finance")

    await store.register_source(hr)
    await store.register_source(finance)

    assert await store.get_source_record("shared.txt", acl_ref="acl:hr") == hr
    assert await store.get_source_record("shared.txt", acl_ref="acl:finance") == finance


@pytest.mark.asyncio
async def test_source_lookup_requires_exact_acl_match() -> None:
    store = NetworkxStore()

    await store.register_source(SourceRecord("shared.txt", "acl:hr"))

    assert await store.get_source_record("shared.txt", acl_ref="acl:hr") == SourceRecord(
        "shared.txt", "acl:hr"
    )

    assert await store.get_source_record("shared.txt", acl_ref="acl:finance") is None

    assert await store.get_source_record("shared.txt", acl_ref=None) is None
