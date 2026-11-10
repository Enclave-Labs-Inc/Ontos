"""Secure, individually scoped local file loading for M3-3.

Authorization and trusted-registry preflight must run before invoking
these functions. No directory-wide discovery or remote parsing.
"""

from __future__ import annotations

import os
import stat
from io import BytesIO
from pathlib import Path

from ontos.ingest.base import SourceDocument
from ontos.storage.sources import SourceRecord

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 200
DEFAULT_MAX_PDF_CHARS = 2_000_000

_TEXT_EXTENSIONS = frozenset({".txt", ".md"})
_PDF_EXTENSIONS = frozenset({".pdf"})


class ScopedSourceFileError(ValueError):
    """The requested source cannot be loaded safely."""


def _validated_parts(
    source_id: str,
    *,
    allowed_extensions: frozenset[str],
) -> list[str]:
    """Require a normalized root-relative source ID."""
    if not source_id or len(source_id) > 4096 or chr(0) in source_id or chr(92) in source_id:
        raise ScopedSourceFileError("Scoped source cannot be loaded")

    parts = source_id.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ScopedSourceFileError("Scoped source cannot be loaded")

    if Path(parts[-1]).suffix.lower() not in allowed_extensions:
        raise ScopedSourceFileError("Unsupported scoped source format")

    return parts


def _read_scoped_bytes(
    *,
    source_root: Path,
    source_id: str,
    allowed_extensions: frozenset[str],
    max_bytes: int,
) -> bytes:
    """Open one bounded regular file without following child symlinks.

    The source root is explicitly selected by the trusted operator.
    Every child component is opened relative to a directory descriptor.
    """
    parts = _validated_parts(
        source_id,
        allowed_extensions=allowed_extensions,
    )

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    directory_fd: int | None = None
    file_fd: int | None = None

    try:
        directory_fd = os.open(
            source_root.resolve(strict=True),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )

        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd

        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )

        details = os.fstat(file_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
            raise ScopedSourceFileError("Scoped source cannot be loaded")

        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            data = handle.read(max_bytes + 1)

        if len(data) > max_bytes:
            raise ScopedSourceFileError("Scoped source cannot be loaded")

        return data

    except OSError:
        raise ScopedSourceFileError("Scoped source cannot be loaded") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


async def load_scoped_text_file(
    *,
    source_root: Path,
    identity: SourceRecord,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SourceDocument:
    """Read exactly one authorized UTF-8 text/Markdown document."""
    data = _read_scoped_bytes(
        source_root=source_root,
        source_id=identity.source_id,
        allowed_extensions=_TEXT_EXTENSIONS,
        max_bytes=max_bytes,
    )

    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ScopedSourceFileError("Scoped source cannot be loaded") from None

    return SourceDocument(
        source_id=identity.source_id,
        text=text,
        metadata={"source_path": identity.source_id},
        acl_ref=identity.acl_ref,
    )


async def load_scoped_pdf_file(
    *,
    source_root: Path,
    identity: SourceRecord,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    max_chars: int = DEFAULT_MAX_PDF_CHARS,
) -> SourceDocument:
    """Parse one authorized PDF from secured bytes, locally only."""
    if (
        not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or max_pages < 1
        or not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or max_chars < 1
    ):
        raise ValueError("PDF limits must be positive integers")

    data = _read_scoped_bytes(
        source_root=source_root,
        source_id=identity.source_id,
        allowed_extensions=_PDF_EXTENSIONS,
        max_bytes=max_bytes,
    )

    try:
        from pypdf import PdfReader
    except ImportError:
        raise ScopedSourceFileError("Local PDF parser is unavailable") from None

    try:
        # BytesIO ensures pypdf never reopens the original path.
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ScopedSourceFileError("Scoped PDF cannot be parsed")

        page_count = len(reader.pages)
        if page_count == 0 or page_count > max_pages:
            raise ScopedSourceFileError("Scoped PDF exceeds page limits")

        parts: list[str] = []
        char_count = 0

        for page in reader.pages:
            page_text = page.extract_text() or ""
            char_count += len(page_text)
            if char_count > max_chars:
                raise ScopedSourceFileError("Scoped PDF exceeds text limits")
            parts.append(page_text)

        text = "\n\n".join(parts).strip()

    except ScopedSourceFileError:
        raise
    except Exception:
        # Parser exceptions can contain sensitive paths or document data.
        raise ScopedSourceFileError("Scoped PDF cannot be parsed") from None

    if not text:
        raise ScopedSourceFileError("Scoped PDF has no extractable text")

    return SourceDocument(
        source_id=identity.source_id,
        text=text,
        metadata={
            "source_path": identity.source_id,
            "parser": "pypdf",
            "page_count": str(page_count),
            "char_count": str(len(text)),
        },
        acl_ref=identity.acl_ref,
    )


async def load_scoped_document(
    *,
    source_root: Path,
    identity: SourceRecord,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SourceDocument:
    """Dispatch one exact source to a sovereign local parser."""
    if Path(identity.source_id).suffix.lower() == ".pdf":
        return await load_scoped_pdf_file(
            source_root=source_root,
            identity=identity,
            max_bytes=max_bytes,
        )

    return await load_scoped_text_file(
        source_root=source_root,
        identity=identity,
        max_bytes=max_bytes,
    )
