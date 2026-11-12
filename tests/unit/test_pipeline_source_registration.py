"""M3-3: source registration during ingestion."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontos.extraction import ExtractionInput, ExtractionResult
from ontos.ingest import TextConnector
from ontos.ontology import load_ontology
from ontos.pipeline import IngestPipeline
from ontos.storage.networkx_store import NetworkxStore
from ontos.storage.sources import SourceRecord

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


class EmptyExtractor:
    id = "empty-test-extractor"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def extract(self, input: ExtractionInput) -> ExtractionResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("extraction failed")
        return ExtractionResult(facts=[], warnings=[])


def _pipeline(
    store: NetworkxStore,
    extractor: EmptyExtractor,
    acl: str | None,
) -> IngestPipeline:
    mapping = {"doc.txt": acl} if acl is not None else None
    return IngestPipeline(
        connector=TextConnector(
            {"doc.txt": "A document with no accepted facts."},
            acl_ref_by_source=mapping,
        ),
        extractor=extractor,
        resolver=None,
        store=store,
        ontology=load_ontology(STARTER),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("acl", [None, "acl:finance"])
async def test_zero_fact_extraction_registers_source(
    tmp_path: Path,
    acl: str | None,
) -> None:
    path = tmp_path / "store.pkl"
    store = NetworkxStore(path=path)
    report = await _pipeline(store, EmptyExtractor(), acl).run()

    assert report.documents_extracted == 1
    assert report.facts_written == 0
    await store.close()

    restored = NetworkxStore(path=path)
    assert await restored.get_source_record("doc.txt", acl_ref=acl) == SourceRecord("doc.txt", acl)
    await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_acl", "second_acl"),
    [
        ("acl:hr", "acl:finance"),
        (None, "acl:finance"),
        ("acl:finance", None),
    ],
)
async def test_same_id_in_separate_acl_scopes(
    first_acl: str | None,
    second_acl: str | None,
) -> None:
    store = NetworkxStore()
    extractor = EmptyExtractor()

    first = await _pipeline(store, extractor, first_acl).run()
    second = await _pipeline(store, extractor, second_acl).run()

    assert first.documents_extracted == 1
    assert second.documents_extracted == 1

    assert await store.get_source_record("doc.txt", acl_ref=first_acl) == SourceRecord(
        "doc.txt", first_acl
    )

    assert await store.get_source_record("doc.txt", acl_ref=second_acl) == SourceRecord(
        "doc.txt", second_acl
    )


@pytest.mark.asyncio
async def test_failed_extraction_does_not_register_source() -> None:
    store = NetworkxStore()

    with pytest.raises(RuntimeError, match="extraction failed"):
        await _pipeline(
            store,
            EmptyExtractor(fail=True),
            "acl:finance",
        ).run()

    assert await store.get_source_record("doc.txt", acl_ref="acl:finance") is None


@pytest.mark.asyncio
async def test_repeated_ingestion_is_idempotent() -> None:
    store = NetworkxStore()
    extractor = EmptyExtractor()

    first = await _pipeline(store, extractor, "acl:finance").run()
    second = await _pipeline(store, extractor, "acl:finance").run()

    assert first.documents_extracted == 1
    assert second.documents_extracted == 1
    assert extractor.calls == 2
    assert await store.get_source_record("doc.txt", acl_ref="acl:finance") == SourceRecord(
        "doc.txt", "acl:finance"
    )
