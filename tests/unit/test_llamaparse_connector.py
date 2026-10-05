"""LlamaParsePdfConnector — mocked-API tests. No live LlamaCloud calls."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ontos.ingest import Connector, ConnectorError, LlamaParsePdfConnector, SourceDocument
from ontos.ingest.llamacloud_pdf import _reset_bridge_warning_latch_for_tests


class _StubDocument:
    """Mimics what llama_cloud_services' Document carries — just `.text`."""

    def __init__(self, text: str) -> None:
        self.text = text


class _StubLlamaParse:
    """Captures constructor args; aload_data returns a scripted list."""

    last_init_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = kwargs
        self._pages = kwargs.pop("_pages", [_StubDocument("# page one\n\nhello")])

    async def aload_data(self, path: str) -> list[_StubDocument]:  # noqa: ARG002
        return list(self._pages)


class _EmptyLlamaParse(_StubLlamaParse):
    async def aload_data(self, path: str) -> list[_StubDocument]:  # noqa: ARG002
        return []


class _RaisingLlamaParse(_StubLlamaParse):
    async def aload_data(self, path: str) -> list[_StubDocument]:  # noqa: ARG002
        raise RuntimeError("401 Unauthorized: bad api key")


def _install_stub_module(parser_cls: type[_StubLlamaParse]) -> None:
    """Insert a fake ``llama_cloud_services`` module exposing LlamaParse."""
    mod = types.ModuleType("llama_cloud_services")
    mod.LlamaParse = parser_cls  # type: ignore[attr-defined]
    sys.modules["llama_cloud_services"] = mod


@pytest.fixture(autouse=True)
def _reset_bridge_latch() -> None:
    _reset_bridge_warning_latch_for_tests()


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    p = tmp_path / "sample.pdf"
    p.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    return p


def test_connector_conforms_to_protocol(pdf_path: Path) -> None:
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    assert isinstance(conn, Connector)


def test_connector_rejects_empty_api_key(pdf_path: Path) -> None:
    with pytest.raises(ConnectorError, match="non-empty api_key"):
        LlamaParsePdfConnector([pdf_path], api_key="")


def test_connector_id_and_source_kind_are_stable(pdf_path: Path) -> None:
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    assert conn.id == "ontos.ingest.llamaparse-connector"
    assert conn.source_kind == "pdf-llamaparse"


async def test_connector_yields_markdown_text(pdf_path: Path) -> None:
    _install_stub_module(_StubLlamaParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    docs = [d async for d in conn.iter_documents()]
    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, SourceDocument)
    assert doc.source_id == "sample.pdf"
    assert "page one" in doc.text
    assert doc.metadata["parser"] == "llamaparse"
    assert doc.metadata["result_type"] == "markdown"
    assert doc.metadata["page_count"] == "1"


async def test_connector_concatenates_multi_page_pdf(pdf_path: Path) -> None:
    class _MultiPageParse(_StubLlamaParse):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["_pages"] = [_StubDocument("page1"), _StubDocument("page2")]
            super().__init__(**kwargs)

    _install_stub_module(_MultiPageParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    doc = await anext(conn.iter_documents())
    assert doc.text == "page1\n\npage2"
    assert doc.metadata["page_count"] == "2"


async def test_connector_raises_on_zero_documents(pdf_path: Path) -> None:
    _install_stub_module(_EmptyLlamaParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    with pytest.raises(ConnectorError, match="zero documents"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_raises_on_empty_text(pdf_path: Path) -> None:
    class _EmptyTextParse(_StubLlamaParse):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["_pages"] = [_StubDocument("   "), _StubDocument("")]
            super().__init__(**kwargs)

    _install_stub_module(_EmptyTextParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    with pytest.raises(ConnectorError, match="empty text"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_wraps_llamaparse_errors(pdf_path: Path) -> None:
    _install_stub_module(_RaisingLlamaParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    with pytest.raises(ConnectorError, match="401 Unauthorized"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_raises_clear_error_when_dep_missing(
    pdf_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hide any installed/stubbed module so the lazy import fails cleanly.
    monkeypatch.setitem(sys.modules, "llama_cloud_services", None)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake")
    with pytest.raises(ConnectorError, match="enclave-ontos\\[llama\\]"):
        async for _ in conn.iter_documents():
            pass


async def test_connector_carries_acl_ref_and_custom_metadata(pdf_path: Path) -> None:
    _install_stub_module(_StubLlamaParse)
    conn = LlamaParsePdfConnector(
        [pdf_path],
        api_key="llx-fake",
        acl_ref_by_source={"sample.pdf": "acl:finance"},
        metadata_by_source={"sample.pdf": {"filing_date": "2026-01-15"}},
    )
    doc = await anext(conn.iter_documents())
    assert doc.acl_ref == "acl:finance"
    assert doc.metadata["filing_date"] == "2026-01-15"
    # Defaults still populated alongside the caller's metadata.
    assert doc.metadata["parser"] == "llamaparse"


async def test_connector_passes_result_type_to_llamaparse(pdf_path: Path) -> None:
    _install_stub_module(_StubLlamaParse)
    conn = LlamaParsePdfConnector([pdf_path], api_key="llx-fake", result_type="text")
    _ = [d async for d in conn.iter_documents()]
    assert _StubLlamaParse.last_init_kwargs["result_type"] == "text"
    assert _StubLlamaParse.last_init_kwargs["api_key"] == "llx-fake"
