# Graph visualization and export

Ontos stores the operational knowledge graph behind the `GraphStore`
contract. Issue #32 adds an operator-facing visualization and export
surface without changing the operational graph schema.

## Neo4j connection

Use the Neo4j backend by setting:

```bash
export ONTOS_STORAGE_BACKEND=neo4j
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD='...'
export NEO4J_DATABASE=neo4j
```

`NEO4J_USER` defaults to `neo4j` and `NEO4J_DATABASE` defaults to
`neo4j`.

`--storage-path` and `ONTOS_STORAGE_PATH` apply only to the local
NetworkX development store and are ignored for the Neo4j backend.

Neo4j Desktop, Bloom, or Aura should connect to the separate
visualization database populated by `ontos export neo4j --push`, not
to Ontos's operational Neo4j database.

## Operational graph vs export graph

The operational Neo4j schema is deliberately uniform:

```text
(:Entity)-[:RELATES]->(:Entity)
```

Entity type is stored as the node's `type` property. The ontology
predicate is stored on the `RELATES` relationship as `predicate`.

This shape is optimized for permission-aware traversal and temporal
filtering. Exported visualization graphs use a different, lossy
presentation schema:

```text
(:Company)-[:ACQUIRED]->(:Company)
(:Person)-[:WORKS_AT]->(:Company)
```

The export schema is for visualization and interchange only. Do not
point a Neo4j export push at the operational Ontos database.

## Raw entity IDs

Export preserves the graph exactly as stored.

Resolver candidates with different raw IDs remain separate nodes even
when a resolver records that they refer to the same entity. Facts keep
their original raw endpoint IDs.

For example:

```text
company:acme-1
company:acme-2
```

remain two nodes in the exported graph.

Canonical fact rewriting is intentionally out of scope for #32.

Resolver merge records are exported as non-authoritative `MERGED_WITH`
relationships rather than collapsing the raw nodes.

## Temporal views

The default export contains only currently active facts.

`--as-of <timestamp>` produces the graph that was valid at that point
in time.

`--include-closed` produces the full historical/audit view, including
facts whose validity window has been closed.

`--as-of` and `--include-closed` represent different views and should
not be combined.

Exported fact metadata preserves:

- `t_valid`
- `t_invalid`
- `superseded_by`
- `acl_ref`
- confidence
- `source_id`
- extractor identity/version where available

## Permissions

Visualization and export follow the same deny-by-default permission
rules as search and traversal.

A forbidden entity must not appear as a node, count, placeholder, or
"hidden" marker. A fact whose subject or object is not visible to the
caller must not appear either.

Permission filtering happens inside the storage read path before the
graph reaches an exporter.

The normal export path drops unauthorized graph elements completely.

`--include-acl-mask` is implemented as a privileged, audit-logged ACL-bypass export.
must be authorized before any otherwise-forbidden graph structure is
read and is not equivalent to normal user export.

## Bloom presentation

Recommended captions:

- Node caption: `canonical_name`, falling back to `id`
- Relationship caption: ontology predicate
- Node detail: raw `id`, `type`, aliases, source/extractor provenance
- Relationship detail: `source_id`, extractor id/version, confidence,
  `t_valid`, `t_invalid`, `superseded_by`

Bloom exposes `t_valid` and `t_invalid` as relationship properties for
temporal inspection and filtering. Ontos does not claim a Perspective-level
timeline binding: Bloom Perspectives define categories, relationships,
styling, and saved Cypher, while filters belong to individual Scenes.

ACL-restricted and public scenes must be built from permission-filtered
exports. Do not create a scene that reveals the existence of forbidden
nodes.

## Example perspectives

### Starter

The starter ontology is intentionally small. A useful Bloom perspective
shows:

```text
Person -[works_at]-> Company
Company -[acquired]-> Company
Company -[subsidiary_of]-> Company
```

Suggested node labels:

- `Person`
- `Company`

Suggested relationship labels:

- `WORKS_AT`
- `ACQUIRED`
- `SUBSIDIARY_OF`

### Fintech

A filings-focused perspective can emphasize:

```text
Company -[filed]-> Filing
Filing -[contains_section]-> FilingSection
FilingSection -[discloses_risk]-> Risk
Company -[audited_by]-> Auditor
Company -[regulated_by]-> Regulator
Company -[operates_model]-> Model
Model -[governed_by]-> ModelControl
```

A governance-focused scene can add:

```text
Person -[serves_as_director_of]-> Company
Person -[serves_as_officer_of]-> Company
Person -[sits_on_committee]-> BoardCommittee
```

### Pharma

A clinical-development perspective can emphasize:

```text
Company -[sponsors_trial]-> ClinicalTrial
ClinicalTrial -[conducted_by]-> CRO
Person -[leads_trial]-> ClinicalTrial
ClinicalTrial -[evaluates]-> Drug
ClinicalTrial -[treats_indication]-> Indication
ClinicalTrial -[reports_event]-> AdverseEvent
```

A regulatory/manufacturing scene can add:

```text
Company -[submitted]-> RegulatorySubmission
RegulatorySubmission -[reviewed_by]-> Regulator
RegulatorySubmission -[covers_drug]-> Drug
Drug -[manufactured_at]-> ManufacturingSite
ManufacturingSite -[operated_by]-> Company
ManufacturingSite -[inspected_by]-> Regulator
```

## Export formats

Issue #32 provides these export surfaces:

```text
ontos export neo4j
ontos export graphml
ontos export svg
ontos export json-ld
```

`ontos export neo4j` writes `cypher.txt` by default. The generated
Cypher creates the visualization schema with ontology-derived node
labels and predicate-derived relationship types.

It can also push the same visualization schema directly to a separate
Neo4j database:

```text
ontos export neo4j --push bolt://localhost:7687 \
  --user neo4j \
  --password PASSWORD \
  --database ontos-viz
```

Do not use the operational Ontos Neo4j database as the visualization
target.

GraphML is intended for tools such as Gephi, Cytoscape, and yEd.

JSON-LD is the structured interchange representation and can also feed
snapshot-oriented consumers.

SVG is a static visualization fallback for small graphs.

All export commands support temporal and neighborhood selection with
`--as-of`, `--include-closed`, `--entity`, and `--hops`.

The exporters consume the backend-neutral `GraphStore` scan surface
rather than accessing backend internals. Direct Neo4j push occurs only
after that scan has been produced and filtered.


## Bloom perspective asset and privileged ACL export

Ontos ships `scripts/bloom-perspective.json` as the example Bloom
Perspective for the entity labels used by the `starter`, `fintech`, and
`pharma` example ontologies.

The Perspective exposes:

- entity categories using the persisted ontology entity type;
- `canonical_name` as the preferred node caption, with `id` available as
  the fallback identifier;
- predicate-derived relationship types with `predicate` available as the
  relationship caption;
- provenance, confidence, ACL, and bitemporal properties in the Bloom
  inspector;
- saved searches for public facts, ACL-scoped facts, active facts, and
  closed historical facts.

Bloom's Scene filters are scene-local rather than stored in a Perspective,
so `scripts/bloom-perspective.json` cannot pre-wire persistent Scene filters.
Instead, the supplied Perspective ships saved Cypher searches for public,
ACL-scoped, active, and closed historical facts. Operators can use those
searches as deterministic starting views and then apply Scene filters to
`acl_ref`, `t_valid`, or `t_invalid`.

Bloom's documented relationship styling supports color, thickness, and
rule-based styles, but not a persistent dashed-line pattern. Ontos therefore
does not pretend to encode "closed facts dashed" in the Perspective. Closed
facts remain distinguishable through `t_invalid` and the saved historical-facts
search.

### One-shot Neo4j / Bloom setup

Configure the Ontos operational Neo4j store through the existing runtime
configuration:

```bash
export ONTOS_STORAGE_BACKEND=neo4j
export NEO4J_URI=bolt://ontos-neo4j:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=...
export NEO4J_DATABASE=neo4j
```

The operational database uses Ontos's uniform internal schema:

```text
(:Entity)-[:RELATES]->(:Entity)
```

Do **not** point Bloom at a typed visualization schema written into that
same operational database.

Instead, push the ACL-filtered visualization snapshot to a separate Neo4j
database or instance:

```bash
ontos export neo4j \
  --push bolt://bloom-neo4j:7687 \
  --user neo4j \
  --password ... \
  --database ontos-viz
```

The visualization target uses ontology-derived node labels and
predicate-derived relationship types. Pushes are `MERGE`-based and
idempotent.

Connect Bloom to that visualization database, open the Perspective
gallery, and import:

```text
scripts/bloom-perspective.json
```

If the target database schema changes, refresh the Perspective after the
next export so Bloom discovers newly introduced properties or relationship
types.

### Permission-aware exports

The default export path is deny-by-default. Restricted facts are omitted
entirely when the caller is not authorized to view their ACL. Ontos does
not emit hidden-node markers, counts, placeholders, or gaps that reveal
the existence of forbidden facts.

Use `--acting-on-behalf-of` to resolve the normal caller's `view`
permissions:

```bash
ontos export graphml \
  --acting-on-behalf-of user:alice \
  --output graph.graphml
```

`--include-acl-mask` is deliberately different. It is a privileged audit
export that crosses normal ACL boundaries while preserving `acl_ref`
stamps in the exported data:

```bash
ontos export graphml \
  --acting-on-behalf-of user:auditor \
  --include-acl-mask \
  --output audit.graphml
```

The flag requires the caller to hold the dedicated `audit_exporter`
authorization relation. With OpenFGA, the authorization model must expose
that relation for the export object (`acl:export`). The command fails
closed when the role is absent.

Every successful `--include-acl-mask` scan writes a persistent Article-12
audit record before the exported snapshot is returned to the renderer.
The record includes the caller, `include_acl_mask=true`, exported fact
IDs, the allow decision, and fact counts grouped by ACL bucket.

Because this privileged path requires persistent asynchronous audit
storage, configure an async audit URL and signing key, for example:

```bash
export ONTOS_AUDIT_DB_URL=sqlite+aiosqlite:///./audit-logs/audit.sqlite
export ONTOS_AUDIT_SIGNING_KEY='replace-with-secret-key'
```

For PostgreSQL use a `postgresql+asyncpg://...` URL.

Normal exports do not use the ACL-bypass path and do not require the
`audit_exporter` role.

### Temporal export views

By default, exports contain the active view only.

Use an historical point-in-time snapshot:

```bash
ontos export graphml \
  --as-of 2026-01-01T00:00:00+00:00 \
  --output historical.graphml
```

Or include closed facts for an audit-oriented full history:

```bash
ontos export graphml \
  --include-closed \
  --output full-history.graphml
```

`--as-of` and `--include-closed` are mutually exclusive.

The pushed Neo4j relationships preserve `t_valid`, `t_invalid`,
`superseded_by`, provenance, confidence, and ACL metadata so Bloom can
inspect and filter those properties directly.

### Scope

Issue #32 intentionally does not add a `--storage neo4j://...` shortcut,
a built-in HTML viewer, or community-detection visualization modes.
Storage continues to use `RuntimeConfig` and the existing environment
configuration.
