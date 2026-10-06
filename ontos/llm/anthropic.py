"""AnthropicBackend — bridge adapter behind the `anthropic` optional extra.

BRIDGE. Sending prompts to api.anthropic.com violates the in-VPC
sovereignty promise; use it for dev, prototyping, and hosted
deployments that disclose Anthropic as a sub-processor — never in a
regulated in-VPC customer deploy. `Enclave Scribe` (in development)
replaces this once its extraction and planning benchmarks pass.

Uses Claude structured outputs via the SDK's `messages.parse` with the
same Pydantic response models as the OpenAI adapter, so both adapters
accept and reject identical model output. Server-side refusal
fallbacks are enabled: if the primary model declines a request on
policy grounds, the API reruns it on a fallback model in the same
call. Only a refusal from the whole chain surfaces as an error.
"""

from __future__ import annotations

from typing import Any

from ontos.extraction import RawTriple
from ontos.llm._structured import _ExtractionResponse, _PlanResponse, _reify_plan
from ontos.llm.prompts import extraction_prompt, planner_prompt
from ontos.llm.usage import LLMUsage
from ontos.ontology import Ontology
from ontos.planner import RawPlan

DEFAULT_MODEL = "claude-opus-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicRefusalError(RuntimeError):
    """Claude (and every configured fallback) declined the request.

    Distinct from transport errors so callers can report "the model
    refused this document" instead of a generic backend failure.
    """

    def __init__(self, model: str, category: str | None) -> None:
        self.model = model
        self.category = category
        detail = f" (category: {category})" if category else ""
        super().__init__(f"{model} declined the request{detail}")


class AnthropicBackend:
    """Extraction + planning via Claude structured outputs."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        api_key: str | None = None,
        max_tokens: int = 16000,
        fallbacks: bool = True,
    ) -> None:
        self._model = model
        self._client = client
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._fallbacks = fallbacks
        self.usage = LLMUsage()

    @property
    def id(self) -> str:
        return f"anthropic:{self._model}"

    @property
    def version(self) -> str:
        return self._model

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # pragma: no cover — exercised only when extra missing
            raise RuntimeError(
                "AnthropicBackend requires the `anthropic` optional extra: "
                "`uv sync --extra anthropic` or add `anthropic` to your deploy image."
            ) from exc
        self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def _parse(self, system: str, user: str, output_format: type[Any]) -> Any:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
        }
        if self._fallbacks:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        response = await client.beta.messages.parse(**kwargs)
        self._record_usage(response)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AnthropicRefusalError(
                getattr(response, "model", self._model),
                getattr(details, "category", None),
            )
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"{self._model} hit max_tokens={self._max_tokens} before finishing "
                "structured output; raise max_tokens or send a shorter chunk"
            )
        return response.parsed_output

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage.add(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        )

    async def structured_extract(self, text: str, ontology: Ontology) -> list[RawTriple]:
        system, user = extraction_prompt(text, ontology)
        parsed = await self._parse(system, user, _ExtractionResponse)
        if parsed is None:
            return []
        return list(parsed.triples)

    async def structured_plan(self, question: str, ontology: Ontology) -> RawPlan:
        system, user = planner_prompt(question, ontology)
        parsed = await self._parse(system, user, _PlanResponse)
        if parsed is None:
            raise RuntimeError("Anthropic returned no parsed plan")
        return _reify_plan(parsed)
