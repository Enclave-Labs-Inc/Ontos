"""authz subsystem for ontos.

Public API — nothing outside `ontos.authz` may import OpenFGA / SpiceDB
types. Callers depend only on the names re-exported here so swapping
authz backend is a config change, not a rewrite.
"""

from ontos.authz.base import VIEW, AuthzBackend, AuthzError
from ontos.authz.inmemory import InMemoryAuthz

__all__ = [
    "VIEW",
    "AuthzBackend",
    "AuthzError",
    "InMemoryAuthz",
]
