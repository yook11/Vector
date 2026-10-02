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
    model=ModelTarget(provider="gemini", name="gemini-3.8-flash"),
    # 3.8 Flash の thinking (既定 medium) は出力上限に含まれるため、その分を足す。
    model_settings=ModelSettings(max_output_tokens=10240),
    output_type=DirectAnswerDraft,
    response_schema=None,
)
