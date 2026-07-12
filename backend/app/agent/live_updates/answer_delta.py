"""Direct answer deltaをまとめてRedis Streamへ送る。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

import structlog

from app.agent.live_updates.metrics import record_answer_delta_breaker_open
from app.agent.live_updates.stream import (
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
)

ANSWER_DELTA_FLUSH_INTERVAL_SECONDS = 0.25
ANSWER_DELTA_MAX_CHARACTERS = 512
ANSWER_DELTA_BREAKER_FAILURE_THRESHOLD = 3

logger = structlog.get_logger(__name__)


class _AnswerDeltaPublisher(Protocol):
    async def publish(
        self,
        event: AgentRunLiveStreamAnswerDeltaEvent | AgentRunLiveStreamAnswerResetEvent,
    ) -> str | None: ...


class AgentRunLiveAnswerDeltaReporter:
    """回答断片をcoalesceし、連続障害時はattempt内の配信を止める。"""

    def __init__(
        self,
        publisher: _AnswerDeltaPublisher,
        *,
        run_id: UUID,
        attempt_epoch: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._publisher = publisher
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._buffer = ""
        self._active_generation: int | None = None
        self._closed_generations: set[int] = set()
        self._timer_task: asyncio.Task[None] | None = None
        self._consecutive_publish_failures = 0
        self._breaker_open = False

    async def append(self, *, generation: int, text: str) -> None:
        timer_to_await: asyncio.Task[None] | None = None
        try:
            async with self._lock:
                if (
                    not text
                    or self._breaker_open
                    or not self._activate_generation_locked(generation)
                ):
                    return

                self._buffer += text
                if len(self._buffer) >= ANSWER_DELTA_MAX_CHARACTERS:
                    timer_to_await = self._cancel_timer_locked()
                    await self._flush_sized_chunks_locked(generation)

                if self._buffer and not self._breaker_open:
                    self._start_timer_locked(generation)
        finally:
            await _await_cancelled_timer(timer_to_await)

    async def reset(self, *, generation: int) -> None:
        async with self._lock:
            if self._breaker_open:
                return
            await self._publish_event_locked(
                AgentRunLiveStreamAnswerResetEvent(generation=generation),
                generation=generation,
            )

    async def finish(self, *, generation: int) -> None:
        timer_to_await: asyncio.Task[None] | None = None
        try:
            async with self._lock:
                if self._breaker_open or generation in self._closed_generations:
                    return
                if self._active_generation not in (None, generation):
                    return

                timer_to_await = self._cancel_timer_locked()
                if self._active_generation == generation and self._buffer:
                    await self._flush_pending_locked(generation)
                self._close_generation_locked(generation)
        finally:
            await _await_cancelled_timer(timer_to_await)

    async def abort(self, *, generation: int) -> None:
        timer_to_await: asyncio.Task[None] | None = None
        try:
            async with self._lock:
                if generation in self._closed_generations:
                    return
                if self._active_generation not in (None, generation):
                    return

                timer_to_await = self._cancel_timer_locked()
                self._buffer = ""
                self._close_generation_locked(generation)
        finally:
            await _await_cancelled_timer(timer_to_await)

    def _activate_generation_locked(self, generation: int) -> bool:
        if generation in self._closed_generations:
            return False
        if self._active_generation is None:
            self._active_generation = generation
        return self._active_generation == generation

    def _close_generation_locked(self, generation: int) -> None:
        self._closed_generations.add(generation)
        if self._active_generation == generation:
            self._active_generation = None
        self._buffer = ""

    def _start_timer_locked(self, generation: int) -> None:
        if self._timer_task is not None:
            return
        self._timer_task = asyncio.create_task(
            self._flush_after_interval(generation),
            name="agent-run-answer-delta-flush",
        )

    def _cancel_timer_locked(self) -> asyncio.Task[None] | None:
        task = self._timer_task
        self._timer_task = None
        if task is not None:
            task.cancel()
        return task

    async def _flush_after_interval(self, generation: int) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        try:
            await self._sleep(ANSWER_DELTA_FLUSH_INTERVAL_SECONDS)
            async with self._lock:
                if self._timer_task is not task:
                    return
                self._timer_task = None
                if (
                    self._breaker_open
                    or self._active_generation != generation
                    or not self._buffer
                ):
                    return
                await self._flush_pending_locked(generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if self._timer_task is task:
                    self._timer_task = None

    async def _flush_sized_chunks_locked(self, generation: int) -> None:
        while (
            len(self._buffer) >= ANSWER_DELTA_MAX_CHARACTERS and not self._breaker_open
        ):
            text = self._buffer[:ANSWER_DELTA_MAX_CHARACTERS]
            self._buffer = self._buffer[ANSWER_DELTA_MAX_CHARACTERS:]
            await self._publish_locked(generation, text)

    async def _flush_pending_locked(self, generation: int) -> None:
        while self._buffer and not self._breaker_open:
            text = self._buffer[:ANSWER_DELTA_MAX_CHARACTERS]
            self._buffer = self._buffer[ANSWER_DELTA_MAX_CHARACTERS:]
            await self._publish_locked(generation, text)

    async def _publish_locked(self, generation: int, text: str) -> None:
        await self._publish_event_locked(
            AgentRunLiveStreamAnswerDeltaEvent(
                generation=generation,
                text=text,
            ),
            generation=generation,
        )

    async def _publish_event_locked(
        self,
        event: AgentRunLiveStreamAnswerDeltaEvent | AgentRunLiveStreamAnswerResetEvent,
        *,
        generation: int,
    ) -> None:
        try:
            stream_id = await self._publisher.publish(event)
        except Exception:
            stream_id = None

        if stream_id is not None:
            self._consecutive_publish_failures = 0
            return

        self._consecutive_publish_failures += 1
        if self._consecutive_publish_failures >= ANSWER_DELTA_BREAKER_FAILURE_THRESHOLD:
            self._open_breaker_locked(generation)

    def _open_breaker_locked(self, generation: int) -> None:
        if self._breaker_open:
            return
        self._breaker_open = True
        self._buffer = ""
        logger.warning(
            "agent_run_answer_delta_breaker_open",
            run_id=str(self._run_id),
            attempt_epoch=self._attempt_epoch,
            generation=generation,
        )
        record_answer_delta_breaker_open()


async def _await_cancelled_timer(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    try:
        await task
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
    except Exception:
        return
