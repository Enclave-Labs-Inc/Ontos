"""CardinalitySupersessionPolicy — default supersession rule.

For every new fact, look up its predicate's cardinality in the
ontology. If the object side is singular (`one_to_one` or
`many_to_one` — each subject has one object), decide what to do
with any older active fact for the same `(subject_id, predicate)`
and ACL:

- Same source re-ingests the same `(s, p, o)` → `skip_add`
  (idempotent). Keeps the audit trail from exploding on repeat
  ingests but still records the decision.
- Different source corroborates `(s, p, o)` → the new fact is
  WRITTEN. Preserves the second source's provenance (CLAUDE.md:
  never drop or synthesize provenance metadata).
- New fact is NEWER than existing active candidate → close the
  candidate (standard supersession).
- New fact is OLDER than an existing active candidate (backfill /
  out-of-order ingest) → write the new fact PRE-CLOSED with
  `t_invalid = earliest.t_valid` and `superseded_by = earliest.id`;
  do not touch the newer active candidate. Honest bitemporal
  semantics for historical ingest.

Supersession is strict same-ACL: a public ingest never touches a
confidential prior fact, and vice versa.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ontos.ontology import Ontology
from ontos.runtime.models import Fact
from ontos.storage.base import GraphStore
from ontos.supersession.base import SupersessionDecision, SupersessionRecord

_SINGULAR_OBJECT_CARDINALITIES = frozenset({"one_to_one", "many_to_one"})


class CardinalitySupersessionPolicy:
    """Close older active facts when the ontology says the relation
    allows at most one object per subject. t_valid-aware.
    """

    id: str = "ontos.supersession.cardinality"
    version: str = "0.1.0"

    async def decide(
        self,
        new_fact: Fact,
        store: GraphStore,
        ontology: Ontology,
    ) -> SupersessionDecision:
        relation = ontology.relation_type(new_fact.predicate)
        if relation is None:
            return SupersessionDecision()
        if relation.cardinality not in _SINGULAR_OBJECT_CARDINALITIES:
            return SupersessionDecision()

        # `facts_for_entity` returns both in-edges and out-edges;
        # filter in Python to keep same-subject, same-predicate, same-ACL,
        # still-alive candidates. ACL arguments narrow the read at the
        # store layer so a cross-ACL candidate is never even visible.
        prior = await store.facts_for_entity(
            new_fact.subject_id,
            as_of=None,
            acl_subject=new_fact.acl_ref,
            allowed_acls=[new_fact.acl_ref] if new_fact.acl_ref is not None else None,
        )
        candidates = [
            f
            for f in prior
            if f.subject_id == new_fact.subject_id
            and f.predicate == new_fact.predicate
            and f.t_invalid is None
            and f.acl_ref == new_fact.acl_ref
            and f.id != new_fact.id
        ]
        if not candidates:
            return SupersessionDecision()

        now = datetime.now(UTC)

        # Idempotent re-ingest: same (s, p, o, source_id) already active →
        # skip the write but RECORD the decision so audit can see it.
        same_source = next(
            (
                f
                for f in candidates
                if f.object_id == new_fact.object_id
                and f.provenance.source_id == new_fact.provenance.source_id
            ),
            None,
        )
        if same_source is not None:
            return SupersessionDecision(
                skip_add=True,
                record=SupersessionRecord(
                    new_fact_id=new_fact.id,
                    closed_fact_ids=[],
                    policy_id=self.id,
                    policy_version=self.version,
                    decided_at=now,
                    reason=(
                        f"idempotent re-ingest: identical (subject, predicate, "
                        f"object, source_id={new_fact.provenance.source_id!r}) "
                        f"already active as fact {same_source.id}"
                    ),
                ),
            )

        # Backfill / out-of-order: new fact's t_valid precedes an existing
        # active candidate's. Write the new fact pre-closed by the earliest
        # newer candidate, leaving the newer active fact untouched.
        newer_than_new = [f for f in candidates if f.t_valid > new_fact.t_valid]
        if newer_than_new:
            earliest = min(newer_than_new, key=lambda f: f.t_valid)
            return SupersessionDecision(
                new_fact_t_invalid=earliest.t_valid,
                new_fact_superseded_by=earliest.id,
                record=SupersessionRecord(
                    new_fact_id=new_fact.id,
                    closed_fact_ids=[],
                    policy_id=self.id,
                    policy_version=self.version,
                    decided_at=now,
                    reason=(
                        f"historical ingest: new fact t_valid={new_fact.t_valid} "
                        f"precedes active candidate {earliest.id} "
                        f"(t_valid={earliest.t_valid}); written as pre-closed"
                    ),
                ),
            )

        # Standard supersession: close every candidate that
        #   (a) is at or before the new fact's t_valid, and
        #   (b) names a DIFFERENT object (a real contradiction).
        # Candidates with the same object are corroborations from a
        # different source — leave them active so the second source's
        # provenance survives alongside the new fact.
        to_close = [
            f
            for f in candidates
            if f.t_valid <= new_fact.t_valid and f.object_id != new_fact.object_id
        ]
        if not to_close:
            return SupersessionDecision()
        return SupersessionDecision(
            facts_to_close=to_close,
            record=SupersessionRecord(
                new_fact_id=new_fact.id,
                closed_fact_ids=[f.id for f in to_close],
                policy_id=self.id,
                policy_version=self.version,
                decided_at=now,
                reason=(
                    f"relation {new_fact.predicate!r} has cardinality "
                    f"{relation.cardinality!r}; closing {len(to_close)} "
                    "older same-(subject, predicate) active fact(s)"
                ),
            ),
        )
