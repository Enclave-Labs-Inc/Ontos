"""PypdfConnector — in-VPC, text-only PDF ingest via local ``pypdf``.

Sovereign connector. No network, no API key, no outbound traffic.
Companion to ``LlamaParsePdfConnector`` (BRIDGE); together the pair
makes the operator's compliance posture an explicit choice:

- Pick ``pypdf`` for sovereign in-VPC ingest. Works on PDFs with an
  embedded text layer (typical office / finance / legal filings).
  Zero network calls.
- Pick ``llamaparse`` when the PDF is scanned / image-based and
  needs vision. ``LlamaParsePdfConnector`` is the BRIDGE option
  for exactly this case — document bytes leave the VPC.

``PypdfConnector`` fails loud with an actionable message when it
gets zero characters out of a PDF (image-based), pointing operators
at the ``llamaparse`` complement rather than silently returning
empty text — which would look identical to "nothing to extract"
in the audit trail and lose the ability to prove we tried.

The connector loads ``pypdf`` lazily so the base install stays
slim — install the extra to enable it::

    pip install 'enclave-ontos[pdf]'
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from ontos.ingest.base import ConnectorError, SourceDocument


class PypdfConnector:
    """Yields ``SourceDocument``s whose text is pypdf's extracted layer."""

    id: str = "ontos.ingest.pypdf-connector"
    source_kind: str = "pdf-pypdf"

    def __init__(
        self,
        pdf_paths: list[Path],
        *,
        root: Path | None = None,
        acl_ref_by_source: dict[str, str] | None = None,
        metadata_by_source: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._pdf_paths = list(pdf_paths)
        self._root = root
        self._acl_refs = dict(acl_ref_by_source or {})
        self._metadata = dict(metadata_by_source or {})

    def _source_id_for(self, path: Path) -> str:
        """Stable, collision-free document key.

        When ``root`` is set, use the path relative to it (matches what the
        text slice does in ``_read_source_dir`` so provenance from mixed
        directories is consistent). Falls back to the basename for callers
        that don't pass a root — collisions become the caller's problem to
        avoid, but the CLI always passes ``root``.
        """
        if self._root is not None:
            try:
                return path.relative_to(self._root).as_posix()
            except ValueError:
                # Path isn't actually under root — fall back to basename
                # rather than silently stripping, so this surfaces.
                pass
        return path.name

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        for path in self._pdf_paths:
            source_id = self._source_id_for(path)
            pages, text = _read_pdf(path, source_id=source_id)

            if not text:
                # Loud failure by design — a 0-char PDF is almost always
                # a scanned / image-based document; the operator needs to
                # know to switch to --pdf-backend llamaparse rather than
                # see an empty text layer silently succeed. (The exact
                # wording is pinned by issue #39's acceptance criteria.)
                raise ConnectorError(
                    f"PypdfConnector got zero characters from {path}. "
                    "The PDF is likely image-based; use --pdf-backend "
                    "llamaparse for vision-based parsing."
                )

            caller_metadata = dict(self._metadata.get(source_id, {}))
            caller_metadata.setdefault("parser", "pypdf")
            caller_metadata.setdefault("page_count", str(pages))
            caller_metadata.setdefault("char_count", str(len(text)))
            # Record the RELATIVE path, not str(path): absolute paths would
            # leak customer directory layout + usernames into provenance /
            # audit records. source_id is already the relative path when
            # root is set.
            caller_metadata.setdefault("source_path", source_id)

            yield SourceDocument(
                source_id=source_id,
                text=text,
                metadata=caller_metadata,
                acl_ref=self._acl_refs.get(source_id),
            )


def _read_pdf(path: Path, *, source_id: str) -> tuple[int, str]:
    """Parse a PDF, returning (page_count, extracted_text).

    Lazy-imports ``pypdf`` so the base install doesn't pull it. All
    ``pypdf`` failure modes (encrypted file without password, corrupt
    PDF, I/O errors) get wrapped as ``ConnectorError`` so the ingest
    pipeline's existing error-policy handling sees a typed exception
    rather than raw pypdf internals.
    """
    try:
        import pypdf
    except ImportError as exc:
        raise ConnectorError(
            "pypdf is not installed. Install the extra: pip install 'enclave-ontos[pdf]'"
        ) from exc

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 — wrap everything as ConnectorError
        raise ConnectorError(f"pypdf failed to open {source_id}: {exc!r}") from exc

    try:
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001 — e.g. FileNotDecryptedError
        raise ConnectorError(f"pypdf failed to read pages from {source_id}: {exc!r}") from exc

    text_parts: list[str] = []
    for i, page in enumerate(pages):
        try:
            text_parts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 — page-level recovery
            raise ConnectorError(
                f"pypdf failed to extract text from {source_id} page {i + 1}: {exc!r}"
            ) from exc

    text = "\n\n".join(text_parts).strip()
    return len(pages), text
