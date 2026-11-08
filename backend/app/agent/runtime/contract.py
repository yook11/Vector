"""Provider-neutral Agent runtime contracts."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, Self

from app.agent.agent import Agent

__all__ = [
    "AgentRuntime",
    "AgentRuntimeScopeFactory",
    "AgentTextStream",
    "StreamingAgentRuntime",
    "StreamingAgentRuntimeScopeFactory",
]


class AgentRuntime(Protocol):
    """Agent宣言でproviderを1回呼び、返ったJSON objectを返す1 attempt境界。"""

    async def call[InputT](
        self,
        agent: Agent[InputT, Any],
        input: InputT,
        *,
        attempt_number: int,
    ) -> dict[str, Any]: ...


class AgentRuntimeScopeFactory(Protocol):
    """Provider resource scopeをRuntimeとして貸し出すfactory。"""

    def __call__(self) -> AbstractAsyncContextManager[AgentRuntime]: ...


class AgentTextStream(Protocol):
    """Runtimeが貸し出すclose可能なmodel text fragment列。"""

    def __aiter__(self) -> Self: ...

    async def __anext__(self) -> str: ...

    async def aclose(self) -> None: ...


class StreamingAgentRuntime(Protocol):
    """Agent宣言から1 provider streamを開始できるattempt境界。"""

    def stream_text[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> AgentTextStream: ...


class StreamingAgentRuntimeScopeFactory(Protocol):
    """Provider resource scopeをStreaming Runtimeとして貸し出すfactory。"""

    def __call__(self) -> AbstractAsyncContextManager[StreamingAgentRuntime]: ...
