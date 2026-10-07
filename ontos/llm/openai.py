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

from ontos.extraction import RawTriple
from ontos.llm._sovereignty import (
    enforce_prod_bridge_opt_in,
    warn_bridge_backend_instantiated,
)
from ontos.llm._structured import (
    _ExtractionResponse,
    _PlanResponse,
    _PlanSeed,
    _PlanStep,
    _reify_plan,
)
from ontos.llm.prompts import extraction_prompt, planner_prompt
from ontos.ontology import Ontology
from ontos.planner import RawPlan

__all__ = [
    "OpenAIBackend",
    "_ExtractionResponse",
    "_PlanResponse",
    "_PlanSeed",
    "_PlanStep",
]


class OpenAIBackend:
    """Extraction + planning via OpenAI structured outputs."""

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        backend_id = f"openai:{model}"
        # Enforce FIRST so the sovereignty refusal in ONTOS_ENV=prod
        # is not preceded by a warning announcing an instantiation
        # that never happens.
        enforce_prod_bridge_opt_in(backend_id)
        warn_bridge_backend_instantiated(backend_id)
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
