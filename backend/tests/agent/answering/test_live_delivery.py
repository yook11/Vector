"""ensure_answer_generation_continues の停止契約。"""

from __future__ import annotations

import pytest

from app.agent.answering.live_delivery import ensure_answer_generation_continues
from app.agent.contract import AnswerGenerationStopped
from app.agent.runs.execution import Continue, Stop, StopReason


class _FixedContinuation:
    def __init__(self, result: Continue | Stop) -> None:
        self._result = result

    async def should_continue(self) -> Continue | Stop:
        return self._result


@pytest.mark.asyncio
async def test_ensure_continues_when_decision_is_continue() -> None:
    await ensure_answer_generation_continues(_FixedContinuation(Continue()))


@pytest.mark.asyncio
async def test_ensure_none_continuation_is_noop() -> None:
    await ensure_answer_generation_continues(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [StopReason.NOT_CURRENT, StopReason.DEADLINE_EXCEEDED],
)
async def test_ensure_raises_stopped_with_reason(reason: StopReason) -> None:
    with pytest.raises(AnswerGenerationStopped) as raised:
        await ensure_answer_generation_continues(
            _FixedContinuation(Stop(reason)),
        )
    assert raised.value.reason is reason


def test_answer_generation_stopped_defaults_to_not_current() -> None:
    assert AnswerGenerationStopped().reason is StopReason.NOT_CURRENT
