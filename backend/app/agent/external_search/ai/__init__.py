"""External search LLM adapters."""

from app.agent.external_search.ai.deepseek import (
    DeepSeekEvidenceSelector,
    DeepSeekQueryGenerator,
    ExternalDeepSeekResponseDefect,
)
from app.agent.external_search.ai.prompts import (
    EXTERNAL_EVIDENCE_SELECTOR_PROMPT,
    EXTERNAL_QUERY_GENERATOR_PROMPT,
    DeepSeekEvidenceSelectorPrompt,
    DeepSeekQueryGeneratorPrompt,
)
from app.agent.external_search.ai.schema_tool import (
    EVIDENCE_SELECTOR_TOOL_SCHEMA,
    QUERY_GENERATOR_TOOL_SCHEMA,
)
from app.agent.external_search.ai.spec import (
    DEEPSEEK_EVIDENCE_SELECTOR_SPEC,
    DEEPSEEK_QUERY_GENERATOR_SPEC,
    EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
    ExternalSearchDeepSeekSpec,
)

__all__ = [
    "DEEPSEEK_EVIDENCE_SELECTOR_SPEC",
    "DEEPSEEK_QUERY_GENERATOR_SPEC",
    "EVIDENCE_SELECTOR_TOOL_SCHEMA",
    "EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS",
    "EXTERNAL_EVIDENCE_SELECTOR_PROMPT",
    "EXTERNAL_QUERY_GENERATOR_PROMPT",
    "ExternalSearchDeepSeekSpec",
    "ExternalDeepSeekResponseDefect",
    "DeepSeekEvidenceSelector",
    "DeepSeekEvidenceSelectorPrompt",
    "DeepSeekQueryGenerator",
    "DeepSeekQueryGeneratorPrompt",
    "QUERY_GENERATOR_TOOL_SCHEMA",
]
