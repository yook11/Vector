"""AnswerGenerationStopped の既定理由。"""

from app.agent.running.stop import AnswerGenerationStopped, StopReason


def test_answer_generation_stopped_defaults_to_not_current() -> None:
    assert AnswerGenerationStopped().reason is StopReason.NOT_CURRENT
