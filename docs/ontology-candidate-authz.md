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

## Identity requirements — trusted local operator (M1)

M1 permits candidate inspection only through the local CLI:

`ontos ontology candidates list --acting-on-behalf-of SUBJECT`

This command is intended to run by a trusted operator on the deployment
host. Operating-system and deployment controls must restrict who can
execute the command and access its storage and audit credentials.

The supplied `--acting-on-behalf-of` identity is **operator-asserted**.
It is not independently authenticated or verified against the operating
system user. The Article-12 audit record identifies its assurance level
as `operator_asserted`.

Operator assertion is a deployment trust assumption, not an authentication
security boundary. Anyone with access to run the command and its credentials
could assert another subject. It must not be exposed as a remotely
accessible service or delegated to untrusted callers.

The shared inspection service still requires both `ontology_reviewer`
and source-level `view` permissions. Missing authorization must fail closed.

The MCP `candidates` tool is not registered in M1. The existing MCP
interface accepts caller-supplied identity strings and does not independently
authenticate them. Authentication for the entire MCP surface is tracked
separately in issue #69. Network-accessible candidate inspection must wait
for verified caller identity.

## Audit requirements

Every candidate-inspection attempt, including denials and failures,
must generate an Article-12 audit record.

Production-facing inspection must persist its audit record before
returning candidate data.

Audit metadata must not contain inaccessible candidate identifiers,
raw triples, hidden candidate counts, or forbidden source ACL lists.

## Deployment isolation

Rejected candidates remain outside trusted graph search, traversal, scans,
and visualization exports, including privileged ACL-mask exports.

NetworkX V3 persistence files and operational Neo4j candidate nodes
intentionally contain raw rejected triples. Protect those stores, their
backups, and their credentials as sensitive operational data.

Do not distribute raw NetworkX persistence files as sanitized graph
snapshots. Export only the trusted graph projection.

Bloom and visualization users must not receive unrestricted database
access to operational Neo4j candidate nodes.

The visualization export must target a separate visualization database
containing only the trusted graph projection.

Candidate retention remains opt-in (`--retain-rejected-candidates`, disabled
by default).

**Retention limitation (M1):** There is currently no automatic TTL expiry,
individual candidate deletion command, or purge-on-source-deletion mechanism.
Once enabled, candidate records persist in their separate storage backend until
that storage is deliberately discarded or managed through an external,
deployment-specific process. Operators must account for retained records in
backups and replicas as well.

Do not enable candidate retention for production data unless the deployment
has an appropriate deletion and retention procedure. TTL expiry and
source-deletion propagation remain future work.
