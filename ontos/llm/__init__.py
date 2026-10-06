"""llm subsystem for ontos.

Real-LLM adapters that satisfy both the extraction `LLMBackend` and
the planner `LLMPlannerBackend` shapes. Ollama is the sovereignty
default (local, no auth, in-VPC). OpenAI and Anthropic are BRIDGEs —
not for regulated in-VPC deploys — behind the `openai` and `anthropic`
optional extras.

`Enclave Scribe` replaces both once its extraction and planning
benchmarks pass current frontier models.
"""

from ontos.llm.anthropic import AnthropicBackend, AnthropicRefusalError
from ontos.llm.ollama import OllamaBackend, OllamaTimeoutError
from ontos.llm.openai import OpenAIBackend
from ontos.llm.usage import LLMUsage

__all__ = [
    "AnthropicBackend",
    "AnthropicRefusalError",
    "LLMUsage",
    "OllamaBackend",
    "OllamaTimeoutError",
    "OpenAIBackend",
]
