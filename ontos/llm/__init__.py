"""llm subsystem for ontos.

Real-LLM adapters that satisfy both the extraction `LLMBackend` and
the planner `LLMPlannerBackend` shapes. Ollama is the sovereignty
default (local, no auth, in-VPC). OpenAI is a BRIDGE — dev only, not
for regulated deploys — behind the `openai` optional extra.

`Enclave Scribe` replaces both once its extraction and planning
benchmarks pass current frontier models.
"""

from ontos.llm.ollama import OllamaBackend
from ontos.llm.openai import OpenAIBackend

__all__ = [
    "OllamaBackend",
    "OpenAIBackend",
]
