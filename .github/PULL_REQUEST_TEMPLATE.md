<!--
Thanks for contributing to Ontos!

Please fill in every section below. If a section genuinely doesn't apply,
write "n/a" and one line saying why — don't delete it.

Compliance-critical PRs (anything touching authz, audit, or provenance)
must include tests under tests/compliance/.
-->

## Why

<!-- The problem this PR solves. Link the issue if one exists (Fixes #NNN). -->

## What

<!-- The change, in 3–5 bullets. Reviewers should not have to read the
     diff to know the shape of what changed. -->

## Non-negotiables checklist

<!-- Tick every box that applies. If you had to weaken any invariant,
     explain in "Notes for reviewer" below. -->

- [ ] Every new `Fact` written to the store carries full `Provenance`.
- [ ] Every code path that returns data to an agent emits an `AuditRecord` before returning.
- [ ] No permission post-filtering added; traversal-time checks preserved; no node-existence leaks.
- [ ] No in-place `Fact` mutation; corrections use `close_fact(...)` supersession.
- [ ] No outbound network calls to hosts the operator did not configure; no telemetry to Enclave-operated services.
- [ ] Extractor interface remains a drop-in for `enclave-scribe` — no LlamaIndex/LangChain-specific types leak past `ontos/extraction/`.

## Tests

<!-- List what you added/changed under tests/. Compliance-critical PRs
     MUST land with matching tests under tests/compliance/. -->

- [ ] Unit tests added / updated
- [ ] Compliance tests added / updated (required if this touches authz, audit, or provenance)
- [ ] Integration tests added / updated
- [ ] Benchmarks updated (if this changes retrieval quality or latency)

## Verification

<!-- The exact commands you ran to convince yourself this works. -->

```bash
uv run ruff check ontos tests
uv run mypy ontos
uv run pytest -q
```

## Notes for reviewer

<!-- Anything the reviewer needs to know that isn't obvious from the diff:
     tradeoffs you considered, alternatives you rejected, follow-ups you
     plan for a later PR. -->

## DCO

- [ ] I have signed off every commit (`git commit -s`) per the [DCO](https://developercertificate.org/).
