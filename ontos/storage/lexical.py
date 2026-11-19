"""BM25 lexical ranking shared by every GraphStore backend (#119 phase 1).

Corpus statistics (document count, average length, document frequency)
are computed over the facts the caller may see at the requested time,
never over the whole store. With global statistics a forbidden fact would
change the scores and therefore the ranks of visible ones, which leaks its
existence. This is why the backends score here instead of using a
store-wide full-text index such as Neo4j's Lucene index.

Both backends hand this module the same field lists for the same fact, so
NetworkX and Neo4j return identical scores and orderings.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Hashable, Iterable, Sequence
from typing import Literal

SearchMode = Literal["substring", "lexical"]

# Okapi BM25 defaults.
K1 = 1.2
B = 0.75

# Letters and digits only: underscores split too, so `works_at` matches
# "works" and "at". Unicode-aware, so non-English names tokenize.
_TOKEN_RE = re.compile(r"[^\W_]+")


def tokenize(text: str) -> list[str]:
    """Casefolded word tokens; punctuation, underscores and spaces split."""
    return _TOKEN_RE.findall(text.casefold())


def rank_bm25[Key: Hashable](
    query: str,
    documents: Iterable[tuple[Key, Sequence[str]]],
) -> list[tuple[float, Key]]:
    """Score each (key, fields) document against `query`.

    Returns only documents sharing at least one token with the query,
    unordered; callers sort with their backend-independent tie-break.
    The idf variant is non-negative, so every matching document scores > 0.
    """
    terms = sorted(set(tokenize(query)))
    if not terms:
        return []

    tokenized: list[tuple[Key, Counter[str], int]] = []
    for key, fields in documents:
        tokens = [token for field in fields for token in tokenize(field)]
        tokenized.append((key, Counter(tokens), len(tokens)))

    count = len(tokenized)
    if count == 0:
        return []
    average_length = sum(length for _, _, length in tokenized) / count or 1.0

    document_frequency = {
        term: sum(1 for _, counts, _ in tokenized if term in counts) for term in terms
    }
    idf = {
        term: math.log(1 + (count - df + 0.5) / (df + 0.5))
        for term, df in document_frequency.items()
    }

    scored: list[tuple[float, Key]] = []
    for key, counts, length in tokenized:
        score = 0.0
        matched = False
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            matched = True
            norm = K1 * (1 - B + B * length / average_length)
            score += idf[term] * frequency * (K1 + 1) / (frequency + norm)
        if matched:
            scored.append((score, key))
    return scored
