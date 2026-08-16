"""Evidence Reviewer Agent の宣言。"""

from __future__ import annotations

from typing import Any, Final

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.contract import (
    EVIDENCE_REVIEW_MISSING_LIMIT,
    EVIDENCE_REVIEWER_SELECTION_LIMIT,
)
from app.agent.evidence_review.preparation import EvidenceReviewInput
from app.agent.evidence_review.prompts import (
    EVIDENCE_REVIEWER_INSTRUCTIONS,
    EVIDENCE_REVIEWER_PROMPT_VERSION,
    render_evidence_review_input,
)
from app.agent.evidence_review.selection import EvidenceReviewerDraft

EVIDENCE_REVIEWER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selections", "missing"],
    "properties": {
        "selections": {
            "type": "array",
            "description": "選択肢をindexで参照する採用リスト。",
            "maxItems": EVIDENCE_REVIEWER_SELECTION_LIMIT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["option_index", "claim", "why_selected"],
                "properties": {
                    "option_index": {"type": "integer", "minimum": 0},
                    "claim": {"type": "string"},
                    "why_selected": {"type": "string"},
                },
            },
        },
        "missing": {
            "type": "array",
            "description": "Run全体で確認できなかった点。",
            "maxItems": EVIDENCE_REVIEW_MISSING_LIMIT,
            "items": {"type": "string"},
        },
    },
}

EVIDENCE_REVIEWER_PROMPT = AgentPrompt[EvidenceReviewInput](
    version=EVIDENCE_REVIEWER_PROMPT_VERSION,
    instructions=EVIDENCE_REVIEWER_INSTRUCTIONS,
    input_renderer=render_evidence_review_input,
)

EVIDENCE_REVIEWER_AGENT: Final[Agent[EvidenceReviewInput, EvidenceReviewerDraft]] = (
    Agent(
        name="evidence_reviewer",
        prompt=EVIDENCE_REVIEWER_PROMPT,
        model=ModelTarget(provider="deepseek", name="deepseek-v4-flash"),
        model_settings=ModelSettings(max_output_tokens=16384),
        output_type=EvidenceReviewerDraft,
        response_schema=EVIDENCE_REVIEWER_RESPONSE_SCHEMA,
    )
)
