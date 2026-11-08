"""LLMを1回呼ぶ差し替え口と、口の結果をAgentの出力へ検証する呼び出し。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, Self

from app.agent.agent import Agent
from app.agent.output import validate_output

__all__ = [
    "AgentTextStream",
    "LlmCaller",
    "LlmCallerScopeFactory",
    "LlmTextStreamer",
    "LlmTextStreamerScopeFactory",
    "call_agent",
]


class LlmCaller(Protocol):
    """Agent宣言でproviderを1回呼び、返ったJSON objectを返す1 attempt境界。"""

    async def call[InputT](
        self,
        agent: Agent[InputT, Any],
        input: InputT,
        *,
        attempt_number: int,
    ) -> dict[str, Any]: ...


class LlmCallerScopeFactory(Protocol):
    """Provider resource scopeをLlmCallerとして貸し出すfactory。"""

    def __call__(self) -> AbstractAsyncContextManager[LlmCaller]: ...


class AgentTextStream(Protocol):
    """LlmTextStreamerが貸し出すclose可能なmodel text fragment列。"""

    def __aiter__(self) -> Self: ...

    async def __anext__(self) -> str: ...

    async def aclose(self) -> None: ...


class LlmTextStreamer(Protocol):
    """Agent宣言から1 provider streamを開始できるattempt境界。"""

    def stream_text[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> AgentTextStream: ...


class LlmTextStreamerScopeFactory(Protocol):
    """Provider resource scopeをLlmTextStreamerとして貸し出すfactory。"""

    def __call__(self) -> AbstractAsyncContextManager[LlmTextStreamer]: ...


async def call_agent[InputT, OutputT](
    caller: LlmCaller,
    agent: Agent[InputT, OutputT],
    input: InputT,
    *,
    attempt_number: int,
) -> OutputT:
    """口でLLMを1回呼び、返ったJSON objectを同じAgentの出力の型へ検証して返す。"""
    payload = await caller.call(agent, input, attempt_number=attempt_number)
    return validate_output(agent, payload)
