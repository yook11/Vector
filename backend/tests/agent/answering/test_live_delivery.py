"""AnswerGenerationStopped の既定理由。"""

from app.agent.contract import AnswerGenerationStopped
from app.agent.runs.execution import StopReason


def test_answer_generation_stopped_defaults_to_not_current() -> None:
    assert AnswerGenerationStopped().reason is StopReason.NOT_CURRENT
