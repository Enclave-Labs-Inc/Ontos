"""In-memory AuthzBackend for dev + tests.

DEV/TEST ONLY. Not for regulated deploy — every regulated customer
runs their own OpenFGA / SpiceDB in-VPC, per the sovereignty invariant.
"""

from __future__ import annotations

from collections import defaultdict


class InMemoryAuthz:
    """Dict-of-tuples authz store. Zero infra.

    Grants are held as `{(subject, relation): set(objects)}`. Checks
    are O(1); listing is O(1) on the set.
    """

    id: str = "inmemory-authz"

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], set[str]] = defaultdict(set)

    def grant(self, subject: str, relation: str, object_: str) -> None:
        """Non-async — grants are static setup, not runtime traffic."""
        self._grants[(subject, relation)].add(object_)

    def revoke(self, subject: str, relation: str, object_: str) -> None:
        self._grants[(subject, relation)].discard(object_)

    async def check(self, subject: str, relation: str, object_: str) -> bool:
        return object_ in self._grants.get((subject, relation), set())

    async def list_authorized_objects(
        self, subject: str, relation: str
    ) -> list[str]:
        return sorted(self._grants.get((subject, relation), set()))
