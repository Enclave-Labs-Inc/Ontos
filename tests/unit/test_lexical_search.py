"""#119 phase 1: BM25 lexical search."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.lexical import rank_bm25, tokenize
from ontos.storage.networkx_store import NetworkxStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _provenance(source_id: str = "doc.md") -> Provenance:
    return Provenance(
        source_id=source_id,
        extractor_id="test",
        extractor_version="1",
        confidence=Confidence.EXTRACTED,
        confidence_score=1.0,
    )


def _fact(
    subject: str,
    predicate: str,
    obj: str,
    *,
    n: int,
    acl_ref: str | None = None,
    t_valid: datetime = T0,
    t_invalid: datetime | None = None,
) -> Fact:
    return Fact(
        id=UUID(int=n),
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        provenance=_provenance(),
        t_valid=t_valid,
        t_invalid=t_invalid,
        ingested_at=T0 + timedelta(seconds=n),
        acl_ref=acl_ref,
    )


async def _entity(
    store: NetworkxStore,
    entity_id: str,
    entity_type: str,
    name: str,
    *,
    aliases: list[str] | None = None,
    acl_ref: str | None = None,
) -> None:
    await store.upsert_entity(
        Entity(
            id=entity_id,
            type=entity_type,
            canonical_name=name,
            aliases=aliases or [],
            provenance=_provenance(),
        ),
        acl_ref=acl_ref,
    )


# --- tokenizer ---------------------------------------------------------------


def test_tokenize_splits_snake_case_punctuation_and_casefolds() -> None:
    assert tokenize("Person:dana lee -works_at-> Company:ACME-Corp") == [
        "person",
        "dana",
        "lee",
        "works",
        "at",
        "company",
        "acme",
        "corp",
    ]


def test_tokenize_is_unicode_aware_and_uses_casefold() -> None:
    assert tokenize("Straße Müller") == ["strasse", "müller"]


def test_tokenize_empty_and_symbols_only() -> None:
    assert tokenize("") == []
    assert tokenize(" -> :: __ ") == []


# --- scoring -----------------------------------------------------------------


def test_no_query_tokens_matches_nothing() -> None:
    assert rank_bm25("?!", [("a", ["acme"])]) == []


def test_only_documents_sharing_a_token_are_returned() -> None:
    ranked = rank_bm25("acme", [("a", ["acme corp"]), ("b", ["globex"])])
    assert [key for _, key in ranked] == ["a"]
    assert ranked[0][0] > 0


def test_rare_term_outweighs_common_term() -> None:
    documents = [
        ("rare", ["dana works_at acme"]),
        ("common1", ["sam works_at globex"]),
        ("common2", ["lee works_at initech"]),
    ]
    scores = dict((key, score) for score, key in rank_bm25("dana works", documents))
    assert scores["rare"] > scores["common1"] == scores["common2"]


def test_shorter_document_ranks_higher_for_same_match() -> None:
    documents = [
        ("short", ["acme"]),
        ("long", ["acme holdings international group plc"]),
    ]
    scores = dict((key, score) for score, key in rank_bm25("acme", documents))
    assert scores["short"] > scores["long"]


def test_repeated_query_terms_count_once() -> None:
    documents = [("a", ["acme"]), ("b", ["globex"])]
    assert rank_bm25("acme acme ACME", documents) == rank_bm25("acme", documents)


# --- NetworkxStore.search(mode="lexical") ------------------------------------


@pytest.mark.asyncio
async def test_multi_word_question_finds_facts_substring_misses() -> None:
    store = NetworkxStore()
    await _entity(store, "Person:dana lee", "Person", "Dana Lee")
    await _entity(store, "Company:acme corp", "Company", "Acme Corp", aliases=["Acme"])
    await store.add_fact(_fact("Person:dana lee", "works_at", "Company:acme corp", n=1))

    question = "who works at Acme?"
    assert await store.search(question) == []
    hits = await store.search(question, mode="lexical")
    assert [hit.id for hit in hits] == [UUID(int=1)]


@pytest.mark.asyncio
async def test_lexical_matches_aliases_and_ranks_best_match_first() -> None:
    store = NetworkxStore()
    await _entity(store, "Company:acme corp", "Company", "Acme Corp", aliases=["ACM"])
    await _entity(store, "Company:globex", "Company", "Globex")
    await _entity(store, "Person:sam", "Person", "Sam")
    await store.add_fact(_fact("Person:sam", "works_at", "Company:globex", n=1))
    await store.add_fact(_fact("Company:acme corp", "acquired", "Company:globex", n=2))

    hits = await store.search("ACM acquired", mode="lexical")
    assert [hit.id for hit in hits][0] == UUID(int=2)


@pytest.mark.asyncio
async def test_lexical_respects_as_of_and_excludes_closed_facts() -> None:
    store = NetworkxStore()
    old = _fact("Person:dana", "works_at", "Company:acme", n=1, t_invalid=T0 + timedelta(days=30))
    new = _fact("Person:sam", "works_at", "Company:acme", n=2, t_valid=T0 + timedelta(days=30))
    await store.add_fact(old)
    await store.add_fact(new)

    current = await store.search("acme", mode="lexical")
    past = await store.search("acme", mode="lexical", as_of=T0 + timedelta(days=1))
    assert [hit.id for hit in current] == [new.id]
    assert [hit.id for hit in past] == [old.id]


@pytest.mark.asyncio
async def test_ties_break_by_ingest_order_then_id() -> None:
    store = NetworkxStore()
    for n in (3, 1, 2):
        await store.add_fact(_fact(f"Person:p{n}", "works_at", "Company:acme", n=n))

    hits = await store.search("acme", mode="lexical")
    assert [hit.id for hit in hits] == [UUID(int=1), UUID(int=2), UUID(int=3)]


@pytest.mark.asyncio
async def test_k_limits_results() -> None:
    store = NetworkxStore()
    for n in range(1, 6):
        await store.add_fact(_fact(f"Person:p{n}", "works_at", "Company:acme", n=n))

    assert len(await store.search("acme", mode="lexical", k=2)) == 2


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown search mode"):
        await NetworkxStore().search("acme", mode="vector")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_substring_mode_is_unchanged_default() -> None:
    store = NetworkxStore()
    await store.add_fact(_fact("Person:dana", "works_at", "Company:acme", n=1))

    assert [f.id for f in await store.search("acm")] == [UUID(int=1)]
    # Lexical matches whole tokens only.
    assert await store.search("acm", mode="lexical") == []
