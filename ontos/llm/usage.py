"""Token usage accumulated by a hosted LLM adapter.

Adapters that report usage keep one `LLMUsage` per instance and add to
it on every call. Callers that need per-run numbers (the CLI, a
metering layer) snapshot it before and after a run and take the
difference. The extraction and planner protocols stay unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    def add(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.calls += 1

    def snapshot(self) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            calls=self.calls,
        )

    def __sub__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens - other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens - other.cache_creation_input_tokens
            ),
            calls=self.calls - other.calls,
        )
