"""External Query Agent の宣言。"""

from __future__ import annotations

from typing import Any, Final

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_TASK_QUERY_LIMIT,
    ExternalQueryDraft,
    ExternalQueryGenerationInput,
)
from app.agent.evidence_collection.external_search.prompts import (
    EXTERNAL_QUERY_INSTRUCTIONS,
    EXTERNAL_QUERY_PROMPT_VERSION,
    render_external_query_input,
)

EXTERNAL_QUERY_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["queries"],
    "properties": {
        "queries": {
            "type": "array",
            "description": (
                f"1 to {EXTERNAL_TASK_QUERY_LIMIT} short English keyword "
                "queries for external news search."
            ),
            "items": {"type": "string"},
        },
    },
}

EXTERNAL_QUERY_PROMPT = AgentPrompt[ExternalQueryGenerationInput](
    version=EXTERNAL_QUERY_PROMPT_VERSION,
    instructions=EXTERNAL_QUERY_INSTRUCTIONS,
    input_renderer=render_external_query_input,
)

EXTERNAL_QUERY_AGENT: Final[Agent[ExternalQueryGenerationInput, ExternalQueryDraft]] = (
    Agent(
        name="external_query_generator",
        prompt=EXTERNAL_QUERY_PROMPT,
        model=ModelTarget(provider="deepseek", name="deepseek-v4-flash"),
        model_settings=ModelSettings(max_output_tokens=256),
        output_type=ExternalQueryDraft,
        response_schema=EXTERNAL_QUERY_RESPONSE_SCHEMA,
    )
)
