"""Stable entity identity derived from an entity's type and name (#121).

LLMs invent their own entity ids ("2", "fmt_1", "Company:Acme Corp"), and
not consistently: one response can give one entity two ids, and the next
document uses another scheme. Storing those ids split one real-world entity
into several graph nodes, so `ask` refused to pick one. Ids are now derived
from the exact-match resolver's own rule, (type, casefolded canonical name),
so the same entity converges within and across documents.
"""

from __future__ import annotations

import hashlib


def entity_identity_id(entity_type: str, canonical_name: str) -> str:
    """`Type:casefolded name`, e.g. `Company:acme corp`."""
    return f"{entity_type.strip()}:{canonical_name.strip().casefold()}"


def acl_scoped_entity_id(entity_id: str, acl_ref: str | None) -> str:
    """Keep same-named entities under different ACLs apart.

    An entity carries one ACL (#48). Without scoping, "Acme Corp" from a
    Finance document and from a public one would collide and the second
    write would be refused. The suffix is a digest, not the ACL name.
    Public entities keep the bare id.
    """
    if acl_ref is None:
        return entity_id
    scope = hashlib.sha256(acl_ref.encode("utf-8")).hexdigest()[:12]
    return f"{entity_id}@{scope}"
