"""executor subsystem for ontos.

Deterministic multi-hop retrieval. Consumes a validated `Plan` and a
`GraphStore`; ranks facts via Personalized-PageRank + PathRAG-style
flow scoring. Never calls an LLM.
"""

from ontos.executor.base import ExecutionHit, ExecutionResult, Executor
from ontos.executor.deterministic import DeterministicExecutor

__all__ = [
    "DeterministicExecutor",
    "ExecutionHit",
    "ExecutionResult",
    "Executor",
]
