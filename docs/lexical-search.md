# Lexical search (BM25)

`search(..., mode="lexical")` ranks facts with Okapi BM25 (`k1 = 1.2`,
`b = 0.75`). It is available on the MCP `search` tool and on both
storage backends. The default mode is still `substring`.

This is phase 1 of #119: vector search and fusion come in later phases.

## What is matched

Each fact's text is made up of:
- its subject and object ids;
- its predicate;
- the canonical names and aliases of both endpoint entities.

Text is casefolded and split on anything that isn't a letter or digit,
including underscores. So `works_at` matches "works", `Straße` matches
"strasse", and the question "who works at Acme?" matches a
`works_at` fact about "Acme Corp".

Tokens must match whole words. `acm` does not match `acme`; use
`mode="substring"` for prefix-style lookups.

## Permissions

BM25 relies on corpus statistics: the number of documents, their
average length, and how many documents contain each term. Ontos
computes these **only over the facts the caller may see** at the
requested `as_of`:
- the fact's own ACL allows the caller;
- both endpoint entities are visible to the caller;
- the fact is alive at that time.

If store-wide statistics were used, forbidden facts would change the
scores, and therefore the order, of visible results. That would reveal
that the forbidden facts exist. `tests/compliance/test_lexical_search_acl.py`
checks that results and ranks are identical whether or not forbidden
facts exist, on a corpus built so that store-wide statistics *would*
reorder them.

For the same reason, Neo4j does not use a store-wide full-text (Lucene)
index here: Lucene's term statistics would count forbidden facts. Both
backends score in `ontos/storage/lexical.py`, so NetworkX and Neo4j
return identical scores and orderings. Ties break by
`(ingested_at, fact id)` (#102).

## Audit

The MCP `search` tool records `mode` in the Article-12 `tool_arguments`.
No scores, and no counts of filtered facts, are ever recorded.

## Cost

Each query scores every fact visible to the caller.

On NetworkX this is about 3× the cost of substring search:

| Visible facts | Time per query |
| --- | --- |
| 10k | about 85 ms |
| 50k | about 0.5 s |

On Neo4j, the text of the visible facts is transferred to the runtime
for each query.

A per-ACL-scope cached index is the planned optimisation.
