"""Evidence Answer Agent宣言。"""

from typing import Final

from app.agent.agent import Agent, ModelSettings, ModelTarget
from app.agent.answering.evidence_answer.ai.schema_tool import (
    EVIDENCE_ANSWER_GEMINI_SCHEMA,
)
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerInput,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.prompts import EVIDENCE_ANSWER_PROMPT

EVIDENCE_ANSWER_AGENT: Final[Agent[EvidenceAnswerInput, RawEvidenceAnswerDraft]] = (
    Agent(
        name="evidence_answer",
        prompt=EVIDENCE_ANSWER_PROMPT,
        model=ModelTarget(provider="gemini", name="gemini-3.1-flash-lite"),
        model_settings=ModelSettings(
            temperature=0.2,
            max_output_tokens=2048,
        ),
        output_type=RawEvidenceAnswerDraft,
        response_schema=EVIDENCE_ANSWER_GEMINI_SCHEMA,
    )
)
