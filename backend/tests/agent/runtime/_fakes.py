"""AgentRuntime policy tests 用の provider-neutral なtest doubles。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from app.agent.agent import Agent

__all__ = ["AgentRuntimeCall", "GoalKeyedAgentRuntime", "ScriptedAgentRuntime"]


@dataclass(frozen=True, slots=True)
class AgentRuntimeCall:
    """ScriptedAgentRuntime が観測した1 attempt。"""

    agent: Agent[Any, Any]
    input: Any
    attempt_number: int


class ScriptedAgentRuntime:
    """AgentRuntime の結果列を FIFO で返す policy test double。"""

    def __init__(self, outcomes: Sequence[object | BaseException]) -> None:
        self._outcomes: deque[object | BaseException] = deque(outcomes)
        self.calls: list[AgentRuntimeCall] = []

    async def call[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        self.calls.append(
            AgentRuntimeCall(
                agent=agent,
                input=input,
                attempt_number=attempt_number,
            )
        )
        if not self._outcomes:
            raise AssertionError(
                "ScriptedAgentRuntime outcome queue exhausted "
                f"at invocation {len(self.calls)}"
            )
        outcome = self._outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return cast(OutputT, outcome)

    def assert_all_outcomes_consumed(self) -> None:
        """全script消費が保証に必要なtestから明示的に呼ぶ。"""

        remaining = len(self._outcomes)
        if remaining:
            raise AssertionError(
                f"ScriptedAgentRuntime has {remaining} unconsumed outcome(s)"
            )


class GoalKeyedAgentRuntime:
    """research_goal で outcome を返す。同時 fan-out でも対応が入れ替わらない。"""

    def __init__(self, outcomes_by_goal: dict[str, object | BaseException]) -> None:
        self._outcomes_by_goal = outcomes_by_goal
        self.calls: list[AgentRuntimeCall] = []

    async def call[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        self.calls.append(
            AgentRuntimeCall(
                agent=agent,
                input=input,
                attempt_number=attempt_number,
            )
        )
        goal = cast(Any, input).task.research_goal
        try:
            outcome = self._outcomes_by_goal[goal]
        except KeyError as exc:
            raise AssertionError(
                f"GoalKeyedAgentRuntime has no outcome for research_goal={goal!r}"
            ) from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return cast(OutputT, outcome)
