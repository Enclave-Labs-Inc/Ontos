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

Candidate ids and pagination cursors are hashes of guessable content
(source, entities, predicate), so they are never logged directly. The
record's `result_fact_ids` and `tool_arguments.after_cursor_ref` hold an
HMAC-SHA256 keyed with a subkey of the audit signing key
(`audit.derive_key(CANDIDATE_REFERENCE_KEY_LABEL)`, see
`candidate_audit_reference`). Someone holding the deployment key can map a
reference back to a candidate. An audit reader without it cannot confirm a
guessed triple against the log (#72).

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


## M2 ontology suggestions — deterministic and LLM-assisted

The local operator CLI supports two modes:

- **M2a (default):** deterministic `add_pattern` suggestions for
  existing relationship types used with missing ontology patterns.
- **M2b (`--llm-model MODEL`):** first includes deterministic M2a
  results, then asks an approved local Ollama model to classify
  remaining unknown relationship types as `map_to_existing` or
  `new_relation_type` suggestions.

Example:

```bash
ontos ontology suggestions list \
  --acting-on-behalf-of user:reviewer \
  --ontology path/to/ontology.yaml \
  --llm-model gemma3:12b \
  --ollama-timeout 180
```

Omit `--llm-model` to use only deterministic M2a suggestions.
The default Ollama request timeout is 180 seconds; a supplied
timeout must be positive and finite.

### Authorization and identity boundary

Suggestions require `ontology_reviewer` on `ontology` and source
VIEW authorization, just like rejected-candidate inspection.
The subject supplied through `--acting-on-behalf-of` is asserted
by a trusted local operator, not independently authenticated
by the CLI. Only trusted operators should have access to the
command, storage credentials and audit credentials.

Candidates are inspected through the audited inspection service.
Authorization is checked again before generation and after
generation, before results can be returned.

A reviewer without VIEW access must not learn about hidden
candidates through suggestions, counts, model requests or audits.

### Model input and ACL isolation

M2b sends only these fields to the model for each eligible
relationship pattern:

- A request-local integer `candidate_id`.
- The subject entity-type label and object entity-type label.
- The observed safe relationship predicate.
- The existing relationship types permitted for that pair,
  as `allowed_mappings`.

Model requests never include raw entity IDs, canonical names,
source IDs, document text, ACL identifiers, support counts,
storage IDs or supporting-candidate references.

Public evidence (`acl_ref=None`) is processed separately from
restricted ACLs. Different restricted ACLs never share a model
request. When a single ACL contains over 100 distinct patterns,
its evidence is split into batches of at most 100 patterns.
Candidate indexes are local to each batch.

M2b does not send patterns already handled by deterministic M2a.
Untrusted model output is validated against the current ontology.
Support counts, extractor provenance, source ACL and HMAC
support references are computed from authorized evidence, not
accepted from model output. Invalid responses are dropped and
counted. A later generation failure aborts the request rather
than returning partially generated suggestions.

### Sovereign deployment boundary — M2b

**M2b deliberately supports loopback-only Ollama for this PR.**
The operator must have an approved Ollama service available
through the local loopback endpoint, normally port 11434.

Direct connections to shared GPU hosts, Kubernetes Services,
private VPC addresses and arbitrary remote Ollama URLs are
not supported by this CLI. There is no `--ollama-url` option
for M2b.

An operator-controlled Ollama sidecar on the same host is one
supported deployment approach. An appropriately secured SSH
local-port tunnel may also expose a separately hosted Ollama
service through loopback, but the deployment operator is
responsible for securing both ends and validating the actual
destination and transport.

**Loopback is not authentication.** On a shared host, another
local process may be able to listen on port 11434 when the
expected Ollama service is unavailable. Restrict local host
access and verify the ownership of the Ollama service.

M2b disables environment-proxy inheritance and HTTP redirects.
Injected HTTP clients are forbidden. OpenAI, Anthropic,
arbitrary spoofed backends and subclasses are not allowed,
even when bridge-backend opt-in is enabled.

A future separately reviewed change may support explicitly
approved in-VPC endpoints or an additional sovereign Scribe
adapter. Neither is enabled by the current M2b CLI.

### Audits, review and changes to the trusted graph

Suggestion requests, denials and failures are audited using
the Article-12 audit infrastructure. The outcome audit records
the generation mode; for approved M2b backends it also records
the generator ID/version and invalid proposal count.
Candidate evidence and raw model output are not logged.

`OntologySuggestion` output includes its kind, ontology ID and
version, generation timestamp, supporting HMAC references,
support count and applicable extractor provenance. It also
includes optional mapping/new-relationship fields. M2a results
carry null and empty defaults for the new M2b fields.

Suggestions are **review proposals only**. Neither M2a nor M2b
changes ontology YAML, creates a new ontology version, promotes
rejected candidates, or inserts suggestions into the trusted
graph. Administrator approval, migrations and re-ingestion of
affected source documents belong to M3.
