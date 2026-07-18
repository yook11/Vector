"""External Query / Selector Agent の宣言。"""

from __future__ import annotations

from typing import Any, Final

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
    ExternalEvidenceSelectionDraft,
    ExternalEvidenceSelectionInput,
    ExternalQueryDraft,
    ExternalQueryGenerationInput,
)
from app.agent.evidence_collection.external_search.prompts import (
    EXTERNAL_EVIDENCE_SELECTOR_INSTRUCTIONS,
    EXTERNAL_EVIDENCE_SELECTOR_PROMPT_VERSION,
    EXTERNAL_QUERY_INSTRUCTIONS,
    EXTERNAL_QUERY_PROMPT_VERSION,
    render_external_evidence_selection_input,
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

EXTERNAL_EVIDENCE_SELECTOR_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selections", "missing"],
    "properties": {
        "selections": {
            "type": "array",
            "description": (
                "Useful candidates only, at most "
                f"{EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK}. "
                "Empty if none are useful."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_index", "claim", "why_selected"],
                "properties": {
                    "candidate_index": {"type": "integer", "minimum": 0},
                    "claim": {"type": "string"},
                    "why_selected": {"type": "string"},
                },
            },
        },
        "missing": {
            "type": "array",
            "description": (
                f"At most {EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK} short "
                "Japanese notes on what could not be confirmed."
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

EXTERNAL_EVIDENCE_SELECTOR_PROMPT = AgentPrompt[ExternalEvidenceSelectionInput](
    version=EXTERNAL_EVIDENCE_SELECTOR_PROMPT_VERSION,
    instructions=EXTERNAL_EVIDENCE_SELECTOR_INSTRUCTIONS,
    input_renderer=render_external_evidence_selection_input,
)

EXTERNAL_EVIDENCE_SELECTOR_AGENT: Final[
    Agent[ExternalEvidenceSelectionInput, ExternalEvidenceSelectionDraft]
] = Agent(
    name="external_evidence_selector",
    prompt=EXTERNAL_EVIDENCE_SELECTOR_PROMPT,
    model=ModelTarget(provider="deepseek", name="deepseek-v4-flash"),
    model_settings=ModelSettings(max_output_tokens=2048),
    output_type=ExternalEvidenceSelectionDraft,
    response_schema=EXTERNAL_EVIDENCE_SELECTOR_RESPONSE_SCHEMA,
)
