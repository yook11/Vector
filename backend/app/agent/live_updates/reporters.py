"""Agent run live updateのStream adapter。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from app.agent.contract import (
    AnswerProgressEvent,
    AnswerProgressStage,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamStageEvent,
)

__all__ = ["AgentRunLiveActivityReporter", "AgentRunLiveStageReporter"]


class AgentRunLiveStageReporter:
    def __init__(self, stream_publisher: AgentRunLiveStreamPublisher) -> None:
        self._stream_publisher = stream_publisher

    async def stage_changed(self, stage: AnswerProgressStage) -> None:
        await _publish(self._publish_stream(stage))

    async def _publish_stream(self, stage: AnswerProgressStage) -> None:
        await self._stream_publisher.publish(AgentRunLiveStreamStageEvent(stage=stage))


class AgentRunLiveActivityReporter:
    def __init__(self, stream_publisher: AgentRunLiveStreamPublisher) -> None:
        self._stream_publisher = stream_publisher

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        await _publish(self._publish_stream(event))

    async def _publish_stream(self, event: AnswerProgressEvent) -> None:
        await self._stream_publisher.publish(
            AgentRunLiveStreamActivityEvent(activity=event)
        )


async def _publish(*operations: Awaitable[object]) -> None:
    await asyncio.gather(*operations, return_exceptions=True)
