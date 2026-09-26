"""M4.b — connector Protocol + TextConnector tests."""

from __future__ import annotations

import pytest

from ontos.ingest import Connector, ConnectorError, SourceDocument, TextConnector
from ontos.ingest.slack import SlackConnector


def test_text_connector_conforms_to_protocol() -> None:
    assert isinstance(TextConnector({}), Connector)


def test_source_document_is_frozen() -> None:
    import pydantic

    doc = SourceDocument(source_id="s1", text="hi")
    with pytest.raises(pydantic.ValidationError):
        doc.text = "changed"  # type: ignore[misc]


async def test_text_connector_yields_all_documents() -> None:
    conn = TextConnector({"doc1": "hello", "doc2": "world"})
    docs = [d async for d in conn.iter_documents()]
    assert len(docs) == 2
    ids = {d.source_id for d in docs}
    assert ids == {"doc1", "doc2"}


async def test_text_connector_carries_acl_ref_when_configured() -> None:
    conn = TextConnector(
        {"doc1": "public", "doc2": "hr-only"},
        acl_ref_by_source={"doc2": "acl:hr"},
    )
    docs = {d.source_id: d async for d in conn.iter_documents()}
    assert docs["doc1"].acl_ref is None
    assert docs["doc2"].acl_ref == "acl:hr"


async def test_text_connector_carries_metadata() -> None:
    conn = TextConnector(
        {"doc1": "x"},
        metadata_by_source={"doc1": {"filename": "notes.md", "author": "alice"}},
    )
    doc = await anext(conn.iter_documents())
    assert doc.metadata["filename"] == "notes.md"
    assert doc.metadata["author"] == "alice"


async def test_text_connector_missing_metadata_defaults_empty() -> None:
    conn = TextConnector({"doc1": "x"})
    doc = await anext(conn.iter_documents())
    assert doc.metadata == {}


async def test_text_connector_empty_input_yields_nothing() -> None:
    conn = TextConnector({})
    docs = [d async for d in conn.iter_documents()]
    assert docs == []


def test_source_kind_is_stable_across_instances() -> None:
    c1 = TextConnector({})
    c2 = TextConnector({"doc": "x"})
    assert c1.source_kind == c2.source_kind == "text-fixture"


def test_slack_connector_conforms_to_protocol() -> None:
    # Even the placeholder must satisfy the Protocol so plumbing that
    # accepts a Connector-typed argument compiles.
    conn = SlackConnector("fake-token")
    assert isinstance(conn, Connector)


def test_slack_connector_from_token_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="placeholder"):
        SlackConnector.from_token("xoxb-fake")


async def test_slack_connector_iter_documents_raises_loud() -> None:
    conn = SlackConnector("fake-token")
    with pytest.raises(ConnectorError):
        async for _ in conn.iter_documents():
            pass
