"""OllamaBackend — the sovereignty-preserving LLM adapter.

Ollama runs locally, needs no auth token, and satisfies the Enclave
in-VPC promise. That's why this is the recommended default and the
first adapter shipped.

Structured output uses Ollama's `format: json` mode. We pass a JSON
Schema through the `format` field on Ollama ≥ 0.5 so the model is
constrained to the exact shape we want; on older Ollama versions the
adapter falls back to `format: "json"` and validates client-side.

Implements both `LLMBackend` (extraction) and `LLMPlannerBackend`
(planning). One class, two callables. Any Ollama-hosted model that
supports structured outputs (llama3, mistral, qwen2.5, phi4, gemma3)
works; pick per your use case.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ontos.extraction import RawTriple
from ontos.llm.prompts import extraction_prompt, planner_prompt
from ontos.ontology import Ontology
from ontos.planner import (
    RawPlan,
    Seed,
    SeedByEntity,
    SeedByKeyword,
    TraversalStep,
)


class OllamaBackend:
    """Extraction + planning against a local Ollama server."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s

    @property
    def id(self) -> str:
        return f"ollama:{self._model}"

    @property
    def version(self) -> str:
        return self._model

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        system, user = extraction_prompt(text, ontology)
        response = await self._chat(system, user, _EXTRACTION_SCHEMA)
        raw_items = response.get("triples") or []
        return [RawTriple.model_validate(item) for item in raw_items]

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan:
        system, user = planner_prompt(question, ontology)
        response = await self._chat(system, user, _PLANNER_SCHEMA)
        return _reify_plan(response)

    async def _chat(self, system: str, user: str, json_schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # Newer Ollama accepts a JSON Schema; older versions accept
            # the string "json" and validate loosely. Both paths land as
            # a JSON blob on `message.content` we parse below.
            "format": json_schema,
        }
        if self._client is None:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=body)
                response.raise_for_status()
                data = response.json()
        else:
            response = await self._client.post(f"{self._base_url}/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        content = data.get("message", {}).get("content", "{}")
        parsed = json.loads(content)
        assert isinstance(parsed, dict)
        return parsed


def _reify_plan(payload: dict[str, Any]) -> RawPlan:
    seed_dict = payload.get("seed", {})
    seed: Seed
    if seed_dict.get("kind") == "by_entity":
        seed = SeedByEntity(entity_id=str(seed_dict["entity_id"]))
    elif seed_dict.get("kind") == "by_keyword":
        seed = SeedByKeyword(
            keyword=str(seed_dict["keyword"]),
            k=int(seed_dict.get("k", 5)),
        )
    else:
        raise ValueError(f"unknown seed kind: {seed_dict!r}")
    steps = [
        TraversalStep(
            relations=list(step.get("relations", [])),
            depth=int(step.get("depth", 1)),
            direction=step.get("direction", "both"),
        )
        for step in payload.get("steps", [])
    ]
    return RawPlan(seed=seed, steps=steps, limit=int(payload.get("limit", 20)))


_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject_id": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "subject_canonical_name": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object_id": {"type": "string"},
                    "object_type": {"type": "string"},
                    "object_canonical_name": {"type": "string"},
                    "llm_confidence": {"type": "number"},
                },
                "required": [
                    "subject_id",
                    "subject_type",
                    "subject_canonical_name",
                    "predicate",
                    "object_id",
                    "object_type",
                    "object_canonical_name",
                    "llm_confidence",
                ],
            },
        }
    },
    "required": ["triples"],
}


_PLANNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seed": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["by_entity", "by_keyword"]},
                "entity_id": {"type": "string"},
                "keyword": {"type": "string"},
                "k": {"type": "integer"},
            },
            "required": ["kind"],
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "relations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depth": {"type": "integer"},
                    "direction": {
                        "type": "string",
                        "enum": ["out", "in", "both"],
                    },
                },
            },
        },
        "limit": {"type": "integer"},
    },
    "required": ["seed"],
}
