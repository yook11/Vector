"""Agent run live updateのfan-out adapter。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from app.agent.contract import (
    AnswerEventReporter,
    AnswerProgressEvent,
    AnswerProgressReporter,
    AnswerProgressStage,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamStageEvent,
)

__all__ = ["AgentRunLiveActivityReporter", "AgentRunLiveStageReporter"]


class AgentRunLiveStageReporter:
    def __init__(
        self,
        progress_writer: AnswerProgressReporter,
        stream_publisher: AgentRunLiveStreamPublisher,
    ) -> None:
        self._progress_writer = progress_writer
        self._stream_publisher = stream_publisher

    async def stage_changed(self, stage: AnswerProgressStage) -> None:
        await _fan_out(
            self._progress_writer.stage_changed(stage),
            self._publish_stream(stage),
        )

    async def _publish_stream(self, stage: AnswerProgressStage) -> None:
        await self._stream_publisher.publish(AgentRunLiveStreamStageEvent(stage=stage))


class AgentRunLiveActivityReporter:
    def __init__(
        self,
        list_publisher: AnswerEventReporter,
        stream_publisher: AgentRunLiveStreamPublisher,
    ) -> None:
        self._list_publisher = list_publisher
        self._stream_publisher = stream_publisher

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        await _fan_out(
            self._list_publisher.event_occurred(event),
            self._publish_stream(event),
        )

    async def _publish_stream(self, event: AnswerProgressEvent) -> None:
        await self._stream_publisher.publish(
            AgentRunLiveStreamActivityEvent(activity=event)
        )


async def _fan_out(*operations: Awaitable[object]) -> None:
    await asyncio.gather(*operations, return_exceptions=True)
