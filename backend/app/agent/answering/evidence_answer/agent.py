"""Evidence Answer Agent宣言。"""

from typing import Final

from app.agent.agent import Agent, ModelSettings, ModelTarget
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.prompts import EVIDENCE_ANSWER_PROMPT

EVIDENCE_ANSWER_AGENT: Final[Agent[EvidenceAnswerInput, EvidenceAnswerDraft]] = Agent(
    name="evidence_answer",
    prompt=EVIDENCE_ANSWER_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-3.1-flash-lite"),
    model_settings=ModelSettings(
        temperature=0.2,
        max_output_tokens=8192,
    ),
    output_type=EvidenceAnswerDraft,
    response_schema=None,
)
