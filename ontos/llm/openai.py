"""OpenAIBackend — bridge adapter behind the `openai` optional extra.

BRIDGE. Sending prompts to api.openai.com violates the in-VPC
sovereignty promise; use it for dev and prototyping, never in a
regulated customer deploy. `Enclave Scribe` (in development)
replaces this once its extraction and planning benchmarks pass.

Uses OpenAI's `chat.completions.parse` with Pydantic response models
(structured outputs). Every completion is constrained to the exact
shape the caller expects — no JSON-parse-repair fallback, no silent
empty.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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


class _ExtractionResponse(BaseModel):
    triples: list[RawTriple]


class _PlanSeed(BaseModel):
    kind: str
    entity_id: str | None = None
    keyword: str | None = None
    k: int | None = None


class _PlanStep(BaseModel):
    relations: list[str] = []
    depth: int = 1
    direction: str = "both"


class _PlanResponse(BaseModel):
    seed: _PlanSeed
    steps: list[_PlanStep] = []
    limit: int = 20


class OpenAIBackend:
    """Extraction + planning via OpenAI structured outputs."""

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._client = client
        self._api_key = api_key

    @property
    def id(self) -> str:
        return f"openai:{self._model}"

    @property
    def version(self) -> str:
        return self._model

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised only when extra missing
            raise RuntimeError(
                "OpenAIBackend requires the `openai` optional extra: "
                "`uv sync --extra openai` or add `openai` to your deploy image."
            ) from exc
        self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        system, user = extraction_prompt(text, ontology)
        client = self._get_client()
        completion = await client.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=_ExtractionResponse,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return []
        return list(parsed.triples)

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan:
        system, user = planner_prompt(question, ontology)
        client = self._get_client()
        completion = await client.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=_PlanResponse,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed plan")
        return _reify_plan(parsed)


def _reify_plan(response: _PlanResponse) -> RawPlan:
    seed: Seed
    if response.seed.kind == "by_entity":
        if not response.seed.entity_id:
            raise ValueError("by_entity seed requires entity_id")
        seed = SeedByEntity(entity_id=response.seed.entity_id)
    elif response.seed.kind == "by_keyword":
        if not response.seed.keyword:
            raise ValueError("by_keyword seed requires keyword")
        seed = SeedByKeyword(keyword=response.seed.keyword, k=response.seed.k or 5)
    else:
        raise ValueError(f"unknown seed kind: {response.seed.kind}")
    steps = [
        TraversalStep(
            relations=step.relations,
            depth=step.depth,
            direction=step.direction,  # type: ignore[arg-type]
        )
        for step in response.steps
    ]
    return RawPlan(seed=seed, steps=steps, limit=response.limit)
