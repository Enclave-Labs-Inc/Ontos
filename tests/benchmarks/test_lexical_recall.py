"""#119 phase 1: recall@k of lexical (BM25) vs substring search. Non-gate.

Questions are phrased the way people ask them, not as exact substrings of
stored text, which is where substring search fails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

pytestmark = pytest.mark.benchmark

T0 = datetime(2026, 1, 1, tzinfo=UTC)
K = 5

ENTITIES = {
    "Person:dana lee": ("Person", "Dana Lee", []),
    "Person:sam ortiz": ("Person", "Sam Ortiz", ["Sammy"]),
    "Person:priya shah": ("Person", "Priya Shah", []),
    "Person:li wei": ("Person", "Li Wei", []),
    "Company:acme corp": ("Company", "Acme Corp", ["Acme"]),
    "Company:globex": ("Company", "Globex Corporation", ["Globex"]),
    "Company:initech": ("Company", "Initech", []),
    "Company:beta labs": ("Company", "Beta Labs", []),
    "Location:berlin": ("Location", "Berlin", []),
}
TRIPLES = [
    ("Person:dana lee", "works_at", "Company:acme corp"),
    ("Person:sam ortiz", "works_at", "Company:acme corp"),
    ("Person:priya shah", "works_at", "Company:globex"),
    ("Person:li wei", "works_at", "Company:initech"),
    ("Company:acme corp", "acquired", "Company:beta labs"),
    ("Company:globex", "partnered_with", "Company:initech"),
    ("Company:beta labs", "located_in", "Location:berlin"),
    ("Person:priya shah", "ceo_of", "Company:globex"),
]
# question -> indexes into TRIPLES that answer it
QUESTIONS = {
    "Who works at Acme?": {0, 1},
    "Which company acquired Beta Labs?": {4},
    "Where is Beta Labs located?": {6},
    "Who is the CEO of Globex?": {7},
    "Globex partners": {5},
    "Sammy employer": {1},
    "Li Wei company": {3},
}


async def _store() -> NetworkxStore:
    store = NetworkxStore()
    for n, (subject, predicate, obj) in enumerate(TRIPLES):
        await store.add_fact(
            Fact(
                id=UUID(int=n),
                subject_id=subject,
                predicate=predicate,
                object_id=obj,
                provenance=Provenance(
                    source_id="corpus.md",
                    extractor_id="benchmark",
                    extractor_version="1",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
                t_valid=T0,
                ingested_at=T0 + timedelta(seconds=n),
            )
        )
    for entity_id, (entity_type, name, aliases) in ENTITIES.items():
        await store.upsert_entity(
            Entity(
                id=entity_id,
                type=entity_type,
                canonical_name=name,
                aliases=aliases,
                provenance=Provenance(
                    source_id="corpus.md",
                    extractor_id="benchmark",
                    extractor_version="1",
                    confidence=Confidence.EXTRACTED,
                    confidence_score=1.0,
                ),
            )
        )
    return store


async def _recall(store: NetworkxStore, mode: str) -> float:
    total = 0.0
    for question, answers in QUESTIONS.items():
        hits = await store.search(question, k=K, mode=mode)  # type: ignore[arg-type]
        found = {hit.id.int for hit in hits} & answers
        total += len(found) / len(answers)
    return total / len(QUESTIONS)


@pytest.mark.asyncio
async def test_lexical_recall_beats_substring(capsys: pytest.CaptureFixture[str]) -> None:
    store = await _store()
    substring = await _recall(store, "substring")
    lexical = await _recall(store, "lexical")

    with capsys.disabled():
        print(f"\nrecall@{K}: substring={substring:.2f} lexical={lexical:.2f}")
    assert lexical > substring
