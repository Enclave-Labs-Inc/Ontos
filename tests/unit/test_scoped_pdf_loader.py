"""M3-3: secure, in-memory, local PDF source loading."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pypdf import PdfWriter

from ontos.ingest.scoped_files import (
    ScopedSourceFileError,
    load_scoped_document,
    load_scoped_pdf_file,
)
from ontos.storage.sources import SourceRecord

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs" / "sample-text.pdf"


@pytest.mark.asyncio
async def test_local_pdf_extraction_preserves_identity_and_acl(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "nested"
    folder.mkdir()
    shutil.copyfile(FIXTURE, folder / "source.pdf")

    identity = SourceRecord("nested/source.pdf", "acl:legal")
    doc = await load_scoped_document(
        source_root=tmp_path,
        identity=identity,
    )

    assert doc.source_id == identity.source_id
    assert doc.acl_ref == "acl:legal"
    assert doc.text.strip()
    assert doc.metadata["parser"] == "pypdf"
    assert int(doc.metadata["page_count"]) >= 1
    assert doc.metadata["source_path"] == "nested/source.pdf"
    assert str(tmp_path) not in doc.metadata["source_path"]


@pytest.mark.asyncio
async def test_text_dispatch_remains_supported(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.md").write_text("Local notes", encoding="utf-8")

    doc = await load_scoped_document(
        source_root=tmp_path,
        identity=SourceRecord("notes.md", None),
    )
    assert doc.text == "Local notes"


@pytest.mark.asyncio
async def test_image_only_pdf_is_rejected(
    tmp_path: Path,
) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)

    with (tmp_path / "blank.pdf").open("wb") as output:
        writer.write(output)

    with pytest.raises(
        ScopedSourceFileError,
        match="no extractable text",
    ):
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("blank.pdf", None),
        )


@pytest.mark.asyncio
async def test_pdf_page_limit_is_enforced(
    tmp_path: Path,
) -> None:
    shutil.copyfile(FIXTURE, tmp_path / "source.pdf")

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("source.pdf", None),
            max_pages=1,
            max_chars=1,
        )


@pytest.mark.asyncio
async def test_pdf_byte_limit_is_enforced(
    tmp_path: Path,
) -> None:
    shutil.copyfile(FIXTURE, tmp_path / "source.pdf")

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("source.pdf", None),
            max_bytes=32,
        )


@pytest.mark.asyncio
async def test_pdf_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    shutil.copyfile(FIXTURE, tmp_path / "original.pdf")
    (tmp_path / "linked.pdf").symlink_to(tmp_path / "original.pdf")

    with pytest.raises(ScopedSourceFileError):
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("linked.pdf", None),
        )


@pytest.mark.asyncio
async def test_corrupt_pdf_has_sanitized_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "corrupt.pdf").write_bytes(b"private internal document content")

    with pytest.raises(
        ScopedSourceFileError,
        match="Scoped PDF cannot be parsed",
    ) as result:
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("corrupt.pdf", None),
        )

    assert "private internal" not in str(result.value)


@pytest.mark.asyncio
async def test_pdf_path_traversal_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ScopedSourceFileError):
        await load_scoped_pdf_file(
            source_root=tmp_path,
            identity=SourceRecord("../outside.pdf", None),
        )


@pytest.mark.asyncio
async def test_unsupported_extension_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ScopedSourceFileError):
        await load_scoped_document(
            source_root=tmp_path,
            identity=SourceRecord("source.docx", None),
        )
