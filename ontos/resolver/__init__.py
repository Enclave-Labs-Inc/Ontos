"""resolver subsystem for ontos.

The entity-resolution cascade. Nothing outside `ontos.resolver` may
import specific resolver-tier types past this file — the cascade
composition is what makes swapping tiers a config change.
"""

from ontos.resolver.base import (
    MergeRecord,
    ResolutionResult,
    Resolver,
    ResolverError,
)
from ontos.resolver.cascade import CascadeResolver
from ontos.resolver.rules import ExactMatchResolver

__all__ = [
    "CascadeResolver",
    "ExactMatchResolver",
    "MergeRecord",
    "ResolutionResult",
    "Resolver",
    "ResolverError",
]
