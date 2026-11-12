"""M3-3 NetworkX source registry persistence and ACL isolation."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord


@pytest.mark.asyncio
async def test_records_survive_restart_without_facts(tmp_path: Path) -> None:
    path = tmp_path / "store.pkl"
    store = NetworkxStore(path=path)

    await store.register_source(SourceRecord("finance.txt", "acl:finance"))
    await store.register_source(SourceRecord("public.txt", None))
    await store.close()

    restored = NetworkxStore(path=path)
    assert await restored.get_source_record("finance.txt", acl_ref="acl:finance") == SourceRecord(
        "finance.txt", "acl:finance"
    )
    assert await restored.get_source_record("public.txt", acl_ref=None) == SourceRecord(
        "public.txt", None
    )
    assert await restored.get_source_record("missing.txt", acl_ref=None) is None
    await restored.close()


@pytest.mark.asyncio
async def test_same_id_different_acls_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "store.pkl"
    store = NetworkxStore(path=path)

    await store.register_source(SourceRecord("shared.txt", "acl:hr"))
    await store.register_source(SourceRecord("shared.txt", "acl:finance"))
    await store.close()

    restored = NetworkxStore(path=path)
    assert await restored.get_source_record("shared.txt", acl_ref="acl:hr") == SourceRecord(
        "shared.txt", "acl:hr"
    )
    assert await restored.get_source_record("shared.txt", acl_ref="acl:finance") == SourceRecord(
        "shared.txt", "acl:finance"
    )
    assert await restored.get_source_record("shared.txt", acl_ref=None) is None
    await restored.close()


@pytest.mark.asyncio
async def test_identical_registration_is_idempotent() -> None:
    store = NetworkxStore()
    record = SourceRecord("doc.txt", "acl:finance")

    await store.register_source(record)
    await store.register_source(record)

    assert await store.get_source_record("doc.txt", acl_ref="acl:finance") == record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        SourceRecord("", None),
        SourceRecord("doc.txt", ""),
    ],
)
async def test_invalid_source_identity_is_rejected(
    record: SourceRecord,
) -> None:
    store = NetworkxStore()
    with pytest.raises(ValueError):
        await store.register_source(record)


@pytest.mark.asyncio
async def test_legacy_snapshot_without_registry_loads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.pkl"
    store = NetworkxStore(path=path)
    store.flush(force=True)
    await store.close()

    header = b"ONTOS-NX-STORE-V3" + bytes([10])
    raw = path.read_bytes()
    assert raw.startswith(header)

    payload = pickle.loads(raw[len(header) :])
    payload.pop("sources", None)
    path.write_bytes(header + pickle.dumps(payload))

    restored = NetworkxStore(path=path)
    assert await restored.get_source_record("legacy.txt", acl_ref=None) is None

    await restored.register_source(SourceRecord("new.txt", None))
    await restored.close()

    reopened = NetworkxStore(path=path)
    assert await reopened.get_source_record("new.txt", acl_ref=None) == SourceRecord(
        "new.txt", None
    )
    await reopened.close()
