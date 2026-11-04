# M3-1: Ontology versioning and migration records

M3-1 adds versioned ontology artifacts, predicate mappings,
declarative identity migrations, and strict query-time migration
of historical facts.

It does not introduce an approval CLI, approve changes automatically,
or re-ingest source documents.

## Ontology changes

An ontology version can be constructed from a migration record
containing additive changes:

- `add_pattern`: permit an additional subject/predicate/object pattern.
- `map_to_existing`: normalize an observed predicate to an existing
  valid predicate for a specific subject/object type pair.
- `new_relation_type`: add a relation type and its corresponding pattern.

Predicate mappings are applied by the LLM extractor before ontology
pattern validation. The canonical predicate is stored in the fact.
The originally observed predicate is retained in
`provenance.mapped_from_predicate`.

Changes are validated against the source ontology. A migration
cannot remove or silently modify existing ontology definitions.

## Version and migration artifacts

`write_ontology_version()` generates two files:

1. A new ontology YAML version.
2. A declarative identity migration YAML record.

The migration record contains:

- `schema_version`
- `ontology_id`
- `from_version` and `to_version`
- `migration_kind: identity`
- `approval_query_id`
- `approved_changes`

Both destination directories must already exist.

The writer refuses to overwrite an existing destination, including
a dangling symlink. It stages complete files before publishing them
and attempts to roll back a partial publication if the second
publication fails.

**Limits:** These are non-overwriting file creation guarantees, not
operating-system-enforced immutability. The two files are not published
as one atomic transaction. Operators must confirm both files are
present and valid before activation and must protect them from later
modification.

## Runtime configuration

Three environment variables control runtime activation:

```bash
export ONTOS_ONTOLOGY_PATH="/secure/ontos/active/ontology-0.2.yaml"
export ONTOS_MIGRATIONS_DIR="/secure/ontos/migrations"
export ONTOS_ONTOLOGY_HISTORY_DIR="/secure/ontos/history"
```

The paths above are examples, not automatically created directories.

The migration directory must contain all migration record YAML
files forming the lineage to the active ontology version.

The history directory must contain earlier ontology YAML versions
required by those records. Do not duplicate the active ontology
version in the history directory.

For example, activating version 0.3 from version 0.1 requires:

```text
active/
  ontology-0.3.yaml

history/
  ontology-0.1.yaml
  ontology-0.2.yaml

migrations/
  0.1-to-0.2.yaml
  0.2-to-0.3.yaml
```

All records must belong to the active ontology's `id`.
Every migration chain must terminate at the active version.
Conflicting edges, incomplete paths, and cycles are rejected.

For each recorded edge, the runtime reconstructs the expected
ontology by applying the declared changes to the historical source
version. It rejects any target version that differs from this
reconstructed result.

`ONTOS_MIGRATIONS_DIR` and `ONTOS_ONTOLOGY_HISTORY_DIR` must be
configured together. Partial or invalid configuration fails
rather than silently disabling verification.

When neither directory is configured, existing legacy executor
behavior is retained.

## Query-time behavior

With verified migrations enabled, the deterministic executor
uses a strict migration registry.

- A fact already on the active ontology version can be read normally.
- A historical fact with a verified migration path remains visible.
- A historical fact without a migration path is excluded and generates
  a migration warning.
- Query-time identity migrations do not rewrite stored facts.

The CLI `ontos query` command uses its explicitly supplied
`--ontology` file as the active ontology.

The MCP server loads and validates its active ontology at startup,
then pins that ontology for subsequent `ask` requests. Activating
a different ontology requires restarting the server.

Storage ACL enforcement remains in place. A migration is not
authorization to bypass source permissions.

## Security and approval boundary

**M3-1 does not independently verify approval authorization.**

The `approval_query_id` field is required and must be a UUID, but
M3-1 does not establish that this UUID refers to a genuine,
authorized Article-12 approval event.

Migration YAML and historical ontology YAML are therefore trusted
operator-managed inputs in M3-1.

Operators must control who can create, modify, replace, and activate
these artifacts. Do not treat a manually constructed migration record
as proof of an authorized ontology change.

M3-2 will introduce the approval workflow, approver authorization,
and approval audit linkage.

M3-3 will introduce operator-triggered, scoped re-ingestion through
the existing audited ingestion path.

Neither ordinary migration loading nor query-time translation
promotes previously rejected extraction candidates into accepted
facts.

## Testing

The default test suite excludes tests marked `integration` or
`benchmark`, as configured in `pyproject.toml`.

For the normal regression gate:

```bash
uv run ruff format --check ontos tests
uv run ruff check .
uv run mypy --strict ontos
uv run pytest -q --tb=short
```

External-service integration tests require their corresponding
services and are run separately in CI.
