"""PypdfConnector — sovereign in-VPC PDF ingest tests. No live network."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from ontos.ingest import Connector, ConnectorError, PypdfConnector, SourceDocument

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs"
TEXT_PDF = FIXTURE_DIR / "sample-text.pdf"  # has an embedded text layer
STUB_PDF = FIXTURE_DIR / "sample.pdf"  # structural stub, 0-char output


def test_connector_conforms_to_protocol() -> None:
    conn = PypdfConnector([TEXT_PDF])
    assert isinstance(conn, Connector)


def test_connector_id_and_source_kind_are_stable() -> None:
    conn = PypdfConnector([TEXT_PDF])
    assert conn.id == "ontos.ingest.pypdf-connector"
    assert conn.source_kind == "pdf-pypdf"


async def test_connector_yields_text_from_vendored_fixture() -> None:
    conn = PypdfConnector([TEXT_PDF], root=FIXTURE_DIR)
    docs = [d async for d in conn.iter_documents()]
    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, SourceDocument)
    assert doc.source_id == "sample-text.pdf"
    assert "Hello Ontos" in doc.text
    assert doc.metadata["parser"] == "pypdf"
    assert doc.metadata["page_count"] == "1"
    assert int(doc.metadata["char_count"]) > 0


async def test_connector_raises_actionable_error_on_zero_char_output() -> None:
    """The exact message from issue #39's acceptance criteria."""
    conn = PypdfConnector([STUB_PDF])
    with pytest.raises(ConnectorError) as exc:
        async for _ in conn.iter_documents():
            pass
    msg = str(exc.value)
    assert "zero characters" in msg
    assert "image-based" in msg
    assert "--pdf-backend llamaparse" in msg
    assert str(STUB_PDF) in msg


async def test_connector_uses_relative_source_id_when_root_set(tmp_path: Path) -> None:
    """Regression against the #28 PR-review collision bug — same-named
    PDFs in different subdirs must get unique source_ids."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    pdf_a = tmp_path / "a" / "report.pdf"
    pdf_b = tmp_path / "b" / "report.pdf"
    pdf_a.write_bytes(TEXT_PDF.read_bytes())
    pdf_b.write_bytes(TEXT_PDF.read_bytes())

    conn = PypdfConnector([pdf_a, pdf_b], root=tmp_path)
    docs = [d async for d in conn.iter_documents()]
    assert len(docs) == 2
    ids = [d.source_id for d in docs]
    assert ids == ["a/report.pdf", "b/report.pdf"]
    assert len(set(ids)) == 2  # explicit uniqueness assertion


async def test_connector_metadata_source_path_is_relative_not_absolute(
    tmp_path: Path,
) -> None:
    """Absolute paths would leak customer directory layout + usernames
    into provenance / audit records."""
    nested_dir = tmp_path / "docs"
    nested_dir.mkdir()
    pdf = nested_dir / "quarterly.pdf"
    pdf.write_bytes(TEXT_PDF.read_bytes())

    conn = PypdfConnector([pdf], root=tmp_path)
    doc = await anext(conn.iter_documents())
    assert doc.metadata["source_path"] == "docs/quarterly.pdf"
    assert str(tmp_path) not in doc.source_id
    assert str(tmp_path) not in doc.metadata["source_path"]


async def test_connector_falls_back_to_basename_without_root(tmp_path: Path) -> None:
    pdf = tmp_path / "notes.pdf"
    pdf.write_bytes(TEXT_PDF.read_bytes())
    conn = PypdfConnector([pdf])  # no root
    doc = await anext(conn.iter_documents())
    assert doc.source_id == "notes.pdf"


async def test_connector_wraps_corrupt_pdf_as_connector_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"not actually a pdf at all")
    conn = PypdfConnector([corrupt])
    with pytest.raises(ConnectorError, match="pypdf failed"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_raises_clear_error_when_dep_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch `sys.modules["pypdf"] = None` so the lazy import raises
    cleanly, same technique used by the LlamaParse connector's missing-dep
    test."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(TEXT_PDF.read_bytes())
    monkeypatch.setitem(sys.modules, "pypdf", None)

    conn = PypdfConnector([pdf])
    with pytest.raises(ConnectorError, match=r"enclave-ontos\[pdf\]"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_carries_acl_ref_and_custom_metadata() -> None:
    conn = PypdfConnector(
        [TEXT_PDF],
        root=FIXTURE_DIR,
        acl_ref_by_source={"sample-text.pdf": "acl:finance"},
        metadata_by_source={"sample-text.pdf": {"filing_date": "2026-01-15"}},
    )
    doc = await anext(conn.iter_documents())
    assert doc.acl_ref == "acl:finance"
    assert doc.metadata["filing_date"] == "2026-01-15"
    # Defaults still populated alongside caller's metadata.
    assert doc.metadata["parser"] == "pypdf"


async def test_connector_wraps_encrypted_pdf_as_connector_error(
    tmp_path: Path,
) -> None:
    """Encrypted PDFs without the password raise FileNotDecryptedError when
    pages are accessed; this gets wrapped as ConnectorError."""
    import pypdf

    # Build an encrypted copy of our fixture.
    writer = pypdf.PdfWriter(clone_from=str(TEXT_PDF))
    writer.encrypt(user_password="secret", owner_password="secret")
    enc_path = tmp_path / "encrypted.pdf"
    with enc_path.open("wb") as f:
        writer.write(f)

    conn = PypdfConnector([enc_path])
    with pytest.raises(ConnectorError, match="pypdf failed"):
        async for _ in conn.iter_documents():
            pass


def test_connector_empty_pdf_list_yields_nothing() -> None:
    """Degenerate empty input — must not crash on construction."""
    conn = PypdfConnector([])
    # No async iteration needed; just prove construction works.
    assert conn._pdf_paths == []  # noqa: SLF001 — explicit shape check


def test_sample_text_fixture_exists_and_is_readable() -> None:
    """Guard: fail loud if the fixture goes missing from the repo."""
    assert TEXT_PDF.exists(), f"vendored {TEXT_PDF.name} fixture is missing"
    assert TEXT_PDF.stat().st_size > 0


def _install_stub_pypdf_module() -> None:
    """Fallback helper if we ever need to simulate pypdf behaviour; kept
    unused today so test_connector_raises_clear_error_when_dep_missing
    uses the simpler monkeypatch-to-None pattern."""
    mod = types.ModuleType("pypdf")
    sys.modules["pypdf"] = mod
