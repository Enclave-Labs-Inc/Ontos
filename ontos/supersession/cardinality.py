"""CardinalitySupersessionPolicy — default supersession rule.

For every new fact, look up its predicate's cardinality in the
ontology. If the object side is singular (`one_to_one` or
`many_to_one` — each subject has one object), close every older
active fact with the same `(subject_id, predicate)` and ACL. Other
cardinalities (`one_to_many`, `many_to_many`) stack naturally and
never trigger supersession.

Re-affirmation (new fact is exactly `(subject, predicate, object)`
of an already-active fact) sets `skip_add=True` so the pipeline
doesn't write a duplicate.

Supersession is strict same-ACL: a public ingest never touches
a confidential prior fact, and vice versa. This preserves the
"ACL as namespace" invariant for regulated deploys.
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
    allows at most one object per subject.
    """

    id: str = "ontos.supersession.cardinality"
    version: str = "0.1.0"

    def __init__(self, ontology: Ontology) -> None:
        self._ontology = ontology

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

        # Fetch existing facts under the same ACL. `facts_for_entity`
        # returns both in-edges and out-edges (networkx_store:135-142),
        # so we filter in Python to keep only the ones with the same
        # subject+predicate+ACL and still alive.
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

        # Re-affirmation — an already-active identical fact exists.
        # Skip the write; the current fact already captures the claim.
        if any(f.object_id == new_fact.object_id for f in candidates):
            return SupersessionDecision(skip_add=True)

        now = datetime.now(UTC)
        return SupersessionDecision(
            facts_to_close=candidates,
            record=SupersessionRecord(
                new_fact_id=new_fact.id,
                closed_fact_ids=[f.id for f in candidates],
                policy_id=self.id,
                policy_version=self.version,
                decided_at=now,
                reason=(
                    f"relation {new_fact.predicate!r} has cardinality "
                    f"{relation.cardinality!r}; each subject allows one "
                    "object at a time"
                ),
            ),
        )
