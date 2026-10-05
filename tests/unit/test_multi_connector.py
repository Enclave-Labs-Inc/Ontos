"""MultiConnector — composition tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ontos.ingest import Connector, ConnectorError, MultiConnector, SourceDocument, TextConnector


class _RaisingConnector:
    """Minimal Connector that always errors — used to prove propagation."""

    id: str = "test.raising"
    source_kind: str = "test"

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        raise ConnectorError("boom")
        yield  # pragma: no cover — keeps the type-checker happy


def test_multi_connector_conforms_to_protocol() -> None:
    assert isinstance(MultiConnector([TextConnector({})]), Connector)


def test_multi_connector_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MultiConnector([])


def test_multi_connector_exposes_sub_connectors() -> None:
    t1 = TextConnector({"a": "1"})
    t2 = TextConnector({"b": "2"})
    multi = MultiConnector([t1, t2])
    assert multi.sub_connectors == [t1, t2]


async def test_multi_connector_fans_out_in_order() -> None:
    t1 = TextConnector({"a1": "alpha", "a2": "alpha2"})
    t2 = TextConnector({"b1": "beta"})
    multi = MultiConnector([t1, t2])

    docs = [d async for d in multi.iter_documents()]
    assert [d.source_id for d in docs] == ["a1", "a2", "b1"]


async def test_multi_connector_yields_nothing_when_sub_connectors_empty() -> None:
    multi = MultiConnector([TextConnector({})])
    docs = [d async for d in multi.iter_documents()]
    assert docs == []


async def test_multi_connector_propagates_sub_connector_errors() -> None:
    multi = MultiConnector([TextConnector({"a": "1"}), _RaisingConnector()])
    collected: list[SourceDocument] = []
    with pytest.raises(ConnectorError, match="boom"):
        async for doc in multi.iter_documents():
            collected.append(doc)
    # Documents emitted BEFORE the raising connector still land.
    assert [d.source_id for d in collected] == ["a"]


def test_multi_connector_id_and_source_kind_are_stable() -> None:
    multi = MultiConnector([TextConnector({})])
    assert multi.id == "ontos.ingest.multi-connector"
    assert multi.source_kind == "multi"
