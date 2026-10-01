"""AuthzBackend — the OpenFGA-drop-in surface.

Ontos never encodes a specific authorization model in the store or in
the MCP tools. The store filters facts against a caller-supplied
`allowed_acls` list; producing that list is the AuthzBackend's job.

The Protocol is small on purpose — anything richer belongs behind an
implementation. OpenFGA and SpiceDB both satisfy this shape, and the
in-memory implementation is dev + test only.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The relation name used when the executor asks "can this subject view
# facts tagged with this acl_ref?" — pinned so callers don't invent
# alternative spellings.
VIEW: str = "view"


@runtime_checkable
class AuthzBackend(Protocol):
    """Narrow authz surface. OpenFGA / SpiceDB / in-memory all adapt to this."""

    @property
    def id(self) -> str: ...

    async def check(self, subject: str, relation: str, object_: str) -> bool:
        """Return True iff `subject` has `relation` on `object_`."""
        ...

    async def list_authorized_objects(self, subject: str, relation: str) -> list[str]:
        """Return the ids of every object the subject has this relation on.

        In OpenFGA this maps directly to the `ListObjects` API. Ontos's
        server uses this once per request to resolve an `acl_subject` into
        a concrete `allowed_acls` list that the store filters against —
        pruning at query time, never post-filter.
        """
        ...


class AuthzError(RuntimeError):
    """Raised when the authz backend itself fails (network, config, etc.).

    A failed authz check is NOT an AuthzError — a failed check is a
    deny. AuthzError is reserved for the backend being unreachable or
    misconfigured, and MUST NOT be swallowed by the request path — a
    request that can't be authorized must fail closed.
    """
