"""M3-3: exact file loading without directory-wide reads."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_APPROVER, VIEW, InMemoryAuthz
from ontos.ingest.scoped_files import (
    ScopedSourceFileError,
    load_scoped_text_file,
)
from ontos.runtime.scoped_reingest import (
    ScopedReingestDeniedError,
    prepare_scoped_reingest,
)
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord


@pytest.mark.asyncio
async def test_loads_exact_requested_file_and_preserves_acl(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "nested"
    folder.mkdir()

    (folder / "selected.md").write_text("Selected content", encoding="utf-8")
    (folder / "unrelated.md").write_text("Unrelated content", encoding="utf-8")

    identity = SourceRecord("nested/selected.md", "acl:legal")

    document = await load_scoped_text_file(
        source_root=tmp_path,
        identity=identity,
    )

    assert document.source_id == identity.source_id
    assert document.text == "Selected content"
    assert document.acl_ref == "acl:legal"
    assert document.metadata["source_path"] == identity.source_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_id",
    [
        "../outside.txt",
        "/absolute.txt",
        "nested/../outside.txt",
        "nested/./selected.txt",
        "nested//selected.txt",
        "nested\\selected.txt",
        "nested/selected.pdf",
        "",
    ],
)
async def test_rejects_unsafe_or_unsupported_source_ids(
    tmp_path: Path,
    source_id: str,
) -> None:
    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord(source_id, None),
        )


@pytest.mark.asyncio
async def test_rejects_symlink_to_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("Private content", encoding="utf-8")

    (tmp_path / "linked.txt").symlink_to(outside)

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord("linked.txt", None),
        )


@pytest.mark.asyncio
async def test_rejects_symlink_directory(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "document.txt").write_text("Private content", encoding="utf-8")
    (tmp_path / "linked").symlink_to(actual, target_is_directory=True)

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord("linked/document.txt", None),
        )


@pytest.mark.asyncio
async def test_rejects_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_bytes(b"x" * 100)

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord("large.txt", None),
            max_bytes=64,
        )


@pytest.mark.asyncio
async def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "invalid.txt").write_bytes(b"\xff\xfe")

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord("invalid.txt", None),
        )


@pytest.mark.asyncio
async def test_rejects_nonregular_file_without_blocking(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_text_file(
            source_root=tmp_path,
            identity=SourceRecord("pipe.txt", None),
        )


@pytest.mark.asyncio
async def test_authorization_precedes_real_file_loading(
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "restricted.txt"
    source_file.write_text("Secret document", encoding="utf-8")

    identity = SourceRecord("restricted.txt", "acl:finance")
    registry = NetworkxStore()
    await registry.register_source(identity)

    authz = InMemoryAuthz()
    audit = AuditEmitter(signing_key=b"m3-3-real-loader-security-test")

    opened: list[SourceRecord] = []

    async def loader(record: SourceRecord):
        opened.append(record)
        return await load_scoped_text_file(
            source_root=tmp_path,
            identity=record,
        )

    # Missing operator role: source loader must never be called.
    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=authz,
            audit=audit,
            subject="user:operator",
            sources=[identity],
            registry=registry,
            load_source=loader,
        )

    assert opened == []

    # Operator role exists, but the source ACL is still denied.
    authz.grant(
        "user:operator",
        ONTOLOGY_APPROVER,
        "ontology",
    )

    with pytest.raises(ScopedReingestDeniedError):
        await prepare_scoped_reingest(
            authz=authz,
            audit=audit,
            subject="user:operator",
            sources=[identity],
            registry=registry,
            load_source=loader,
        )

    assert opened == []

    # Only after the exact ACL grant may the file be loaded.
    authz.grant(
        "user:operator",
        VIEW,
        "acl:finance",
    )

    selected = await prepare_scoped_reingest(
        authz=authz,
        audit=audit,
        subject="user:operator",
        sources=[identity],
        registry=registry,
        load_source=loader,
    )

    assert len(selected) == 1
    assert selected[0].text == "Secret document"
    assert selected[0].acl_ref == "acl:finance"
    assert opened == [identity]
    assert audit.verify_chain()
