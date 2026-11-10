# Ontos demo

One command, about two minutes, fully local:

```bash
ollama serve                 # if it isn't running already
ollama pull llama3           # once
examples/demo/run_demo.sh    # from the repo root
```

Use another local model with `ONTOS_DEMO_MODEL=qwen2.5:7b examples/demo/run_demo.sh`. Output lands in `examples/demo/out/`; open the graph with `open examples/demo/out/graph.svg`.

## Talk track

| Step | What the audience sees | What to say |
|---|---|---|
| 0. Checks | Ollama up, model name | "The model runs on this machine. Nothing leaves the network, so it's deployable air-gapped inside your VPC." |
| 1. Ingest | Two short documents become facts | "Ontos reads your documents and turns them into typed entities and relationships, using an ontology you control." |
| 2. The graph | Each person and company appears **once**, every fact tagged with its source document | "Every fact carries provenance: which document, which model, what confidence. No fact without a source." |
| 3. Ask | "Who works at Acme Corp?" answered with sources | "Plain-English questions are planned and answered from the graph, with each answer traceable to a document." |
| 4. Agent over MCP | An agent lists the tools, calls `explain` and `search`, then reads back the audit record of its own call | "AI agents connect over MCP, the open agent protocol. Every query writes an EU AI Act Article-12 audit record: who asked, on whose behalf, what came back, and when." |
| 5. Audit verify | "audit chain verified ... no tampering detected" | "The audit log is hash-chained. Edit any record and verification fails." |
| 6. Graph | SVG of the knowledge graph | "The same graph exports to Neo4j/Bloom, GraphML or JSON-LD." |

Points worth making that the script doesn't show: per-ACL permissions are enforced on every read (callers never learn that a hidden fact exists), facts are bitemporal (never overwritten), and the ontology evolves through audited, reviewer-approved changes.

## If something looks off

- **A small model makes extraction mistakes.** For example, `llama3` may also record "Dana Lee works at Acme Corp" from the 2026 document even though it says Sam replaced her. That's model quality, not the graph: use a larger local model for important demos. The provenance column shows exactly which document each fact came from, which is the point to make.
- **"model output for a document was unusable; retrying".** A small model sometimes returns malformed output. Re-ingesting is idempotent, so the script retries.
- **Port in use.** `ONTOS_DEMO_PORT=8799 examples/demo/run_demo.sh`.
