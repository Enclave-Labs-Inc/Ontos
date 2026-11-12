# Operator-triggered scoped re-ingestion (M3-3)

## Purpose

`ontos reingest` reprocesses one explicitly selected, previously
registered source under the active ontology.

It supports controlled re-ingestion after an ontology migration,
without performing an unrestricted directory-wide ingestion.

The command is intended for trusted local operators.

## Supported scope

- One source per invocation.
- Exact `(source_id, acl_ref)` registration required.
- `.txt`, `.md`, and locally parsed `.pdf` documents.
- Local Ollama extraction only.
- PDF parsing uses local `pypdf`, not LlamaCloud.
- Existing graph storage: persistent NetworkX or Neo4j.

An unregistered source or mismatched ACL is denied.

## Where the ACL comes from

Local files carry no ACL. The `acl_ref` used by
`ontos reingest` comes from the trusted source registry,
which records the exact `(source_id, acl_ref)` pair a source
was first ingested with.

`--acl-ref` must match that registered pair exactly. It is a
selector, never a way to assign or change a source's ACL.

One source id may be registered under several ACLs; each pair
is a strictly separate scope with its own entities and facts.
Ingesting a source under a new ACL therefore does not replace
or supersede its facts under the old one: ingest reports a
warning ("source also has facts under a different ACL") and
the old facts stay readable under the old ACL. To really
change a source's ACL, retire the old facts explicitly or use
a new source id (an administrative ACL migration is not yet
supported).

## Sources ingested before the registry existed

The registry is populated by normal ingestion since scoped
re-ingestion was added (#120). Sources ingested earlier have facts and
candidates but no registry entry, so `ontos reingest` denies
them.

Register such a source by running a normal `ontos ingest` of
it once with its original ACL. Facts the extractor
reproduces are reaffirmed, not duplicated. An audited backfill command is
tracked in #125.

## Finding the sources to re-ingest

After `ontos ontology approve` writes a signed migration
record, list the sources whose rejected evidence supports the
approved change:

```bash
ontos ontology affected-sources \
  --migration-record migrations/0.1-to-0.2.yaml \
  --acting-on-behalf-of "user:operator" \
  --storage-path /trusted/ontos-store.pkl
```

```json
{
  "ontology_id": "ontos.starter",
  "to_version": "0.2",
  "registered": [{"source_id": "deals/acme.md", "acl_ref": "acl:legal"}],
  "unregistered": [{"source_id": "old/globex.md", "acl_ref": null}]
}
```

- The command requires `ONTOLOGY_APPROVER`, a persistent
  audit database, and the same `ONTOS_AUDIT_SIGNING_KEY` that
  signed the record; unsigned or tampered records are refused.
- Only sources whose evidence the operator may `VIEW` (plus
  public sources) are listed. Nothing about other sources is
  returned, counted, or audited.
- Evidence from any version of the record's ontology counts.
- Pass each `registered` pair to `ontos reingest`.
  `unregistered` sources must be registered first (above).
- The Article-12 record carries only counts and the same keyed
  `scope_ref` that `ontos reingest` records for that set of
  sources, so discovery and re-ingest correlate in the chain.
- The command is read-only.

## Required authorization

The operator must have:

1. `ONTOLOGY_APPROVER` permission on `ontology`.
2. `VIEW` permission on the selected source's ACL when restricted.

Public sources still require the ontology approver permission.

The runtime authorization backend must be configured.

`ONTOS_AUTHZ_BACKEND=openfga` is intended for externally
managed production authorization. The in-memory backend is
for development/tests; its grants are not automatically
persisted or seeded by the CLI.

## Required deployment configuration

The following must be configured before running the command:

| Setting | Purpose |
| --- | --- |
| `ONTOS_AUDIT_DB_URL` | Persistent Article-12 audit database |
| `ONTOS_AUDIT_SIGNING_KEY` | Explicit audit-chain signing key |
| `ONTOS_AUTHZ_BACKEND` | Runtime authorization backend |
| `ONTOS_REINGEST_SOURCE_ROOT` | Trusted source directory |
| `ONTOS_REINGEST_OPERATOR_SUBJECT` | Deployment-pinned operator identity |
| Storage configuration | Existing persistent NetworkX or Neo4j graph |

The supplied `--source-dir` must resolve to the same directory
as `ONTOS_REINGEST_SOURCE_ROOT`.

The supplied `--acting-on-behalf-of` must exactly match
`ONTOS_REINGEST_OPERATOR_SUBJECT`.

**Neither setting authenticates the human operator.**

The deployment must separately enforce authentication,
restrict command execution, protect environment configuration,
and control access to source files and storage credentials.

Users who can modify trusted environment settings may be able
to impersonate the configured operator.

## Example: persistent local development storage

Assuming the operator has already configured a working
authorization backend and registered the selected source:

```bash
export ONTOS_AUDIT_DB_URL="postgresql+asyncpg://..."
export ONTOS_AUDIT_SIGNING_KEY="<secure-signing-key>"
export ONTOS_REINGEST_SOURCE_ROOT="/trusted/corpus"
export ONTOS_REINGEST_OPERATOR_SUBJECT="user:operator"

ontos reingest \
  --source-dir /trusted/corpus \
  --source-id "department/document.md" \
  --acl-ref "acl:finance" \
  --ontology docs/ontology/examples/starter.yaml \
  --acting-on-behalf-of "user:operator" \
  --storage-path /trusted/ontos-store.pkl
```

The example values are placeholders, not deployable credentials.

For Neo4j, configure the existing storage backend instead
of using `--storage-path`. Global storage options must appear
before the subcommand when used.

For a registered public document, omit `--acl-ref`.

## Processing order

1. Initialize the persistent audit sink.
2. Verify the deployment-pinned operator identity.
3. Check ontology approver and per-source VIEW permissions.
4. Validate the configured trusted source root.
5. Initialize the existing graph and source registry.
6. Verify the exact registered source and ACL.
7. Securely load only the selected document.
8. Recheck registration and authorization.
9. Extract using local Ollama.
10. Recheck permissions and registration before graph mutations.
11. Persist storage changes.
12. Record successful completion after persistence.

Failure events are recorded where the audit sink remains
operational. A failed audit operation must not authorize
additional graph writes.

## Document-loading safeguards

Source IDs must be normalized relative paths.

The loader rejects:

- Path traversal.
- Symlinked child files and directories.
- Unsupported extensions.
- Nonregular source files.
- Files exceeding the configured size limit.
- Invalid UTF-8 for text and Markdown.

PDFs are parsed from securely opened bytes, without
reopening the source pathname.

Default limits:

- 10 MiB per input file.
- 200 PDF pages.
- 2,000,000 extracted PDF characters.

Scanned PDFs without extractable text are rejected.

PDF parsing is not currently isolated in a resource-limited
worker process. Only trusted document repositories should be
used until parser isolation is implemented.

## Auditing and confidentiality

The implementation records authorization and execution
decisions in the Article-12 audit chain.

Source scope references are keyed hashes rather than
raw document IDs and ACL references.

The CLI's successful response contains aggregate counts,
not document contents or extracted facts.

Avoid passing sensitive source identifiers on shared systems:
command-line arguments may be visible in process listings,
shell history, or operator logs.

The application should run with appropriately restricted
filesystem permissions and logging controls.

## Failure and consistency limitations

Scoped re-ingestion is **not atomic** across all graph
mutations.

If a failure or permission revocation occurs mid-ingestion,
previously committed writes may remain.

The write gate prevents later mutations after a detected
authorization failure, but does not roll back earlier ones.

NetworkX persistence is checked before the CLI records
completion. Backend failures can still leave uncertain
partial results.

The caller must inspect the audit chain and graph state
after an interrupted run before attempting recovery.

Successful re-ingestion does not automatically delete all
historical facts or prove that ontology migration was
semantically complete.

## Production deployment checklist

- Restrict command invocation to authenticated operators.
- Protect the pinned operator identity and source root.
- Use persistent, access-controlled source storage.
- Use a production authorization backend.
- Use a persistent audit database and securely managed key.
- Use local Ollama and local pypdf only.
- Verify the selected source was registered under the
  expected ACL.
- Ensure backup and recovery procedures exist.
- Review audit records after failures or interruptions.
- Validate storage behavior against the deployed backend.

## Verification

Relevant automated tests include:

- `tests/unit/test_cli_scoped_reingest.py`
- `tests/unit/test_cli_scoped_reingest_success.py`
- `tests/unit/test_scoped_execution.py`
- `tests/unit/test_scoped_file_loader.py`
- `tests/unit/test_scoped_pdf_loader.py`
- `tests/unit/test_pipeline_scoped_write_gate.py`
- `tests/unit/test_scoped_reingest_authorization.py`
- `tests/unit/test_scoped_reingest_preparation.py`
- `tests/unit/test_affected_sources.py`
- `tests/unit/test_ontology_affected_sources_cli.py`
- `tests/compliance/test_affected_sources_acl.py`

The Neo4j integration suite must also pass before M3-3 is
considered ready for review.
