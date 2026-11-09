"""M3-2: ontology approval is a distinct, explicitly granted privilege."""

from ontos.authz import (
    AUDIT_EXPORTER,
    ONTOLOGY_APPROVER,
    ONTOLOGY_REVIEWER,
    VIEW,
    InMemoryAuthz,
)


async def test_existing_roles_do_not_imply_approval() -> None:
    authz = InMemoryAuthz(audit_roles={"user:auditor"})
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:viewer", VIEW, "acl:legal")

    assert await authz.check("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    assert await authz.check("user:auditor", AUDIT_EXPORTER, "ontology")
    assert await authz.check("user:viewer", VIEW, "acl:legal")

    for subject in ("user:reviewer", "user:auditor", "user:viewer"):
        assert not await authz.check(subject, ONTOLOGY_APPROVER, "ontology")


async def test_approver_grant_does_not_imply_review_or_source_view() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")

    assert await authz.check("user:approver", ONTOLOGY_APPROVER, "ontology")
    assert not await authz.check("user:approver", ONTOLOGY_REVIEWER, "ontology")
    assert not await authz.check("user:approver", AUDIT_EXPORTER, "ontology")
    assert await authz.list_authorized_objects("user:approver", VIEW) == []


async def test_approval_grant_is_scoped_to_ontology_object() -> None:
    authz = InMemoryAuthz()
    authz.grant("user:approver", ONTOLOGY_APPROVER, "ontology")

    assert not await authz.check("user:approver", ONTOLOGY_APPROVER, "other")
    assert not await authz.check("user:ungranted", ONTOLOGY_APPROVER, "ontology")
