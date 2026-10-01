"""Shared prompt templates for extraction and planning.

Kept centralized so all backends (OpenAI, Anthropic, Ollama, Scribe)
speak the same prompt shape. Provider-specific structured-output
wrapping (function calls / json_schema / mode) is the backend's
concern; the CONTENT of the prompt lives here.
"""

from __future__ import annotations

from ontos.ontology import Ontology

_EXTRACTION_SYSTEM = """\
You are an information-extraction engine for a compliance-grade
knowledge graph. Extract only relationships that match the schema
below. Never invent entity types or predicates that are not in the
schema — those get dropped and surface as compliance-visible
warnings. Assign an llm_confidence in [0, 1] for each triple:
  1.0  = explicit and unambiguous in the text
  0.7  = strongly implied
  0.4  = inferred with material uncertainty
Return valid JSON matching the requested schema exactly.
"""

_PLANNER_SYSTEM = """\
You are a query planner for a compliance-grade knowledge graph.
Given a natural-language question and a schema, produce a plan that
seeds the traversal from a specific entity or a keyword search and
declares which relations to expand and to what depth. Only reference
entity types and predicates that are in the schema.

When emitting SeedByEntity.entity_id, use the EXACT identity string
as it appears in the graph — typically just the entity's name
(e.g. "Alice Johnson"). Do NOT append the entity type in parentheses
(never "Alice Johnson (Person)"), do NOT wrap in quotes, and do NOT
add articles or other decoration. If you are unsure of the exact
stored id, prefer SeedByKeyword — the executor will substring-search
for you.

Choose traversal direction from the ontology pattern, where relations
are written as:

  subject_type - predicate -> object_type

Direction is relative to the current frontier entity:
- Use direction="out" when traversing from the subject side toward the
  object side.
- Use direction="in" when traversing from the object side toward the
  subject side.
- Use direction="both" only when the question genuinely requires both
  directions or the endpoint role cannot be determined.

For multi-step plans, choose the direction independently for each step
based on which side of that relation the current frontier occupies.

Example: if the schema says Person - works_at -> Company and the
question is "Who works at Acme Corp?", seed Acme Corp and traverse
works_at with direction="in".

Each TraversalStep must be unique — never repeat the same
(relations, depth, direction) combination across steps. A single-hop
query is one step, not the same step twice.

Return valid JSON matching the requested plan schema exactly.
"""


def _schema_summary(ontology: Ontology) -> str:
    """A compact textual summary of the ontology for the LLM's prompt."""
    lines: list[str] = ["Entity types:"]
    for et in ontology.entity_types:
        props = ", ".join(f"{p.name}:{p.type}" for p in et.properties)
        lines.append(f"  - {et.label}({props})")
    lines.append("Relation types:")
    for rt in ontology.relation_types:
        lines.append(f"  - {rt.label} [{rt.cardinality}]")
    lines.append("Allowed patterns (subject_type - predicate -> object_type):")
    for p in ontology.patterns:
        lines.append(f"  - {p.subject_type} - {p.predicate} -> {p.object_type}")
    return "\n".join(lines)


def extraction_prompt(text: str, ontology: Ontology) -> tuple[str, str]:
    """Return (system, user) messages for an extraction call."""
    user = (
        f"Schema:\n{_schema_summary(ontology)}\n\n"
        f"Text to extract from:\n{text}\n\n"
        "Return every triple you can support with the text, using the schema above."
    )
    return _EXTRACTION_SYSTEM, user


def planner_prompt(question: str, ontology: Ontology) -> tuple[str, str]:
    """Return (system, user) messages for a planner call."""
    user = (
        f"Schema:\n{_schema_summary(ontology)}\n\n"
        f"Question:\n{question}\n\n"
        "Produce one plan. Prefer SeedByEntity when the question names a "
        "specific entity; otherwise SeedByKeyword. Choose the smallest "
        "set of steps that could answer the question."
    )
    return _PLANNER_SYSTEM, user
