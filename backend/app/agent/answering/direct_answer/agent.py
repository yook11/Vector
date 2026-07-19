"""Direct Answer Agent宣言。"""

from typing import Final

from app.agent.agent import Agent, ModelSettings, ModelTarget
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.direct_answer.prompts import DIRECT_ANSWER_PROMPT

DIRECT_ANSWER_AGENT: Final[Agent[DirectAnswerInput, DirectAnswerDraft]] = Agent(
    name="direct_answer",
    prompt=DIRECT_ANSWER_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-3.1-flash-lite"),
    model_settings=ModelSettings(
        temperature=0.2,
        max_output_tokens=2048,
    ),
    output_type=DirectAnswerDraft,
    response_schema=None,
)
