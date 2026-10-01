"""Supersession — close older active facts when a newer one contradicts them.

Fixes the bitemporal-correctness gap described in GitHub issue #21:
`IngestPipeline` only called `store.add_fact`, so two facts for
`David Lee works_at ...` ended up both active with `t_invalid=NULL`.
"""

from ontos.supersession.base import (
    NullSupersessionPolicy,
    SupersessionDecision,
    SupersessionPolicy,
    SupersessionRecord,
)
from ontos.supersession.cardinality import CardinalitySupersessionPolicy

__all__ = [
    "CardinalitySupersessionPolicy",
    "NullSupersessionPolicy",
    "SupersessionDecision",
    "SupersessionPolicy",
    "SupersessionRecord",
]
