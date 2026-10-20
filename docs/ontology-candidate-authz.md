# Rejected ontology candidate authorization (M1)

## Access requirements

Candidate inspection requires both:

1. The `ontology_reviewer` relation on the authorization object
   `ontology`.
2. The `view` relation on the source document's `acl_ref`.

The permissions are independent. `audit_exporter` does not grant
`ontology_reviewer`, and document `view` does not grant reviewer access.

## Authorization calls

The inspection service uses:

- `check(subject, "ontology_reviewer", "ontology")`
- `list_authorized_objects(subject, "view")`

The second call returns the ACL objects used for storage-level filtering.

Candidates with `acl_ref=None` represent public-source candidates.
They remain accessible only to authorized ontology reviewers.

## Production OpenFGA requirements

The repository currently supplies an OpenFGA adapter, not a managed
authorization model.

A production OpenFGA model must define `ontology_reviewer` for the
authorization object `ontology`, and retain the existing `view`
permissions on source ACL objects.

The object identifier and object type must match the deployed model.
A Python constant alone does not create an OpenFGA relation.

No reviewer grants should be implicit or inherited from `audit_exporter`.

## Identity requirements

Authorization is meaningful only when `subject` represents an identity
established by a trusted authentication boundary.

The current MCP HTTP interface accepts caller-supplied identity strings.
Those strings must not authorize candidate inspection.

Candidate inspection must remain unavailable through CLI/MCP until a
trusted authentication mechanism is configured and tested.

## Audit requirements

Every candidate-inspection attempt, including denials and failures,
must generate an Article-12 audit record.

Production-facing inspection must persist its audit record before
returning candidate data.

Audit metadata must not contain inaccessible candidate identifiers,
raw triples, hidden candidate counts, or forbidden source ACL lists.

## Deployment isolation

Bloom and visualization users must not receive unrestricted database
access to operational Neo4j candidate nodes.

The visualization export must target a separate visualization database
containing only the trusted graph projection.
