"""M3-3: ACL-scoped selection must fail closed before ingestion."""

from __future__ import annotations

import pytest

from ontos.ingest.base import SourceDocument
from ontos.ingest.scoped import (
    ScopedReingestError,
    select_scoped_sources,
)
from ontos.storage.sources import SourceRecord


def _doc(source: str, acl: str | None) -> SourceDocument:
    return SourceDocument(
        source_id=source,
        text=f"Document {source}",
        acl_ref=acl,
    )


def test_selects_only_requested_sources_in_order() -> None:
    documents = [
        _doc("finance.txt", "acl:finance"),
        _doc("legal.txt", "acl:legal"),
        _doc("public.txt", None),
    ]
    selected = select_scoped_sources(
        requested_sources=[
            SourceRecord("public.txt", None),
            SourceRecord("legal.txt", "acl:legal"),
        ],
        available_documents=documents,
        recorded_sources={
            SourceRecord("finance.txt", "acl:finance"),
            SourceRecord("legal.txt", "acl:legal"),
            SourceRecord("public.txt", None),
        },
    )
    assert [doc.source_id for doc in selected] == [
        "public.txt",
        "legal.txt",
    ]


def test_same_source_id_in_different_acls_is_unambiguous() -> None:
    hr = _doc("shared.txt", "acl:hr")
    finance = _doc("shared.txt", "acl:finance")

    selected = select_scoped_sources(
        requested_sources=[
            SourceRecord("shared.txt", "acl:finance"),
        ],
        available_documents=[hr, finance],
        recorded_sources={
            SourceRecord("shared.txt", "acl:hr"),
            SourceRecord("shared.txt", "acl:finance"),
        },
    )

    assert selected == [finance]


@pytest.mark.parametrize(
    ("original", "available"),
    [
        ("acl:finance", None),
        (None, "acl:finance"),
        ("acl:finance", "acl:legal"),
    ],
)
def test_rejects_wrong_acl(
    original: str | None,
    available: str | None,
) -> None:
    with pytest.raises(
        ScopedReingestError,
        match="preflight failed",
    ):
        select_scoped_sources(
            requested_sources=[SourceRecord("doc.txt", original)],
            available_documents=[_doc("doc.txt", available)],
            recorded_sources={SourceRecord("doc.txt", original)},
        )


def test_rejects_missing_source_file() -> None:
    with pytest.raises(ScopedReingestError, match="preflight failed"):
        select_scoped_sources(
            requested_sources=[SourceRecord("missing.txt", None)],
            available_documents=[_doc("other.txt", None)],
            recorded_sources={SourceRecord("missing.txt", None)},
        )


def test_rejects_missing_trusted_record() -> None:
    with pytest.raises(ScopedReingestError, match="preflight failed"):
        select_scoped_sources(
            requested_sources=[SourceRecord("doc.txt", None)],
            available_documents=[_doc("doc.txt", None)],
            recorded_sources=set(),
        )


def test_rejects_unregistered_acl_scope() -> None:
    with pytest.raises(ScopedReingestError, match="preflight failed"):
        select_scoped_sources(
            requested_sources=[SourceRecord("shared.txt", "acl:finance")],
            available_documents=[_doc("shared.txt", "acl:finance")],
            recorded_sources={SourceRecord("shared.txt", "acl:hr")},
        )


def test_rejects_entire_request_if_one_source_is_invalid() -> None:
    with pytest.raises(ScopedReingestError, match="preflight failed"):
        select_scoped_sources(
            requested_sources=[
                SourceRecord("valid.txt", None),
                SourceRecord("restricted.txt", "acl:restricted"),
            ],
            available_documents=[
                _doc("valid.txt", None),
                _doc("restricted.txt", None),
            ],
            recorded_sources={
                SourceRecord("valid.txt", None),
                SourceRecord("restricted.txt", "acl:restricted"),
            },
        )


@pytest.mark.parametrize(
    "requested",
    [
        [],
        [SourceRecord("", None)],
        [SourceRecord("doc.txt", "")],
        [SourceRecord("doc.txt", None), SourceRecord("doc.txt", None)],
    ],
)
def test_rejects_invalid_scope(
    requested: list[SourceRecord],
) -> None:
    with pytest.raises(ScopedReingestError):
        select_scoped_sources(
            requested_sources=requested,
            available_documents=[_doc("doc.txt", None)],
            recorded_sources={SourceRecord("doc.txt", None)},
        )


def test_rejects_duplicate_connector_identity() -> None:
    with pytest.raises(ScopedReingestError, match="preflight failed"):
        select_scoped_sources(
            requested_sources=[SourceRecord("doc.txt", None)],
            available_documents=[
                _doc("doc.txt", None),
                _doc("doc.txt", None),
            ],
            recorded_sources={SourceRecord("doc.txt", None)},
        )


def test_selects_both_acl_scopes_when_explicitly_requested() -> None:
    hr = _doc("shared.txt", "acl:hr")
    finance = _doc("shared.txt", "acl:finance")

    selected = select_scoped_sources(
        requested_sources=[
            SourceRecord("shared.txt", "acl:hr"),
            SourceRecord("shared.txt", "acl:finance"),
        ],
        available_documents=[finance, hr],
        recorded_sources={
            SourceRecord("shared.txt", "acl:hr"),
            SourceRecord("shared.txt", "acl:finance"),
        },
    )

    assert selected == [hr, finance]
