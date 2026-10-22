"""Authorization contract for ontology candidate reviewers."""

import pytest

from ontos.authz import (
    AUDIT_EXPORTER,
    ONTOLOGY_REVIEWER,
    VIEW,
    InMemoryAuthz,
)


@pytest.mark.asyncio
async def test_reviewer_role_is_separate_from_audit_exporter() -> None:
    authz = InMemoryAuthz(audit_roles={"user:auditor"})

    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    assert await authz.check("user:reviewer", ONTOLOGY_REVIEWER, "ontology")

    assert not await authz.check("user:auditor", ONTOLOGY_REVIEWER, "ontology")

    assert await authz.check("user:auditor", AUDIT_EXPORTER, "export")

    assert not await authz.check("user:reviewer", AUDIT_EXPORTER, "export")

    assert await authz.list_authorized_objects("user:reviewer", VIEW) == ["acl:finance"]

    assert await authz.list_authorized_objects("user:auditor", VIEW) == []


@pytest.mark.asyncio
async def test_view_permission_does_not_grant_reviewer_role() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:employee", VIEW, "acl:finance")

    assert not await authz.check("user:employee", ONTOLOGY_REVIEWER, "ontology")
