"""SSE protocol and connection lifecycle for active agent runs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol
from uuid import UUID

from pydantic.alias_generators import to_camel

from app.agent.live_updates.metrics import (
    AgentRunSseCloseReason,
    record_agent_run_sse_capacity_rejection,
    record_agent_run_sse_close,
    record_agent_run_sse_open,
    record_agent_run_sse_projection_drop,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamAttemptStartedEvent,
    AgentRunLiveStreamEntry,
    AgentRunLiveStreamReadResult,
    AgentRunLiveStreamReadStatus,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.runs.contracts import OwnedAgentRunLiveContext
from app.agent.runs.types import AgentRunStatus

AGENT_RUN_SSE_RUN_CONNECTION_LIMIT = 2
AGENT_RUN_SSE_USER_CONNECTION_LIMIT = 4
AGENT_RUN_SSE_PROCESS_CONNECTION_LIMIT = 50
AGENT_RUN_SSE_LEASE_TTL_SECONDS = 55.0
AGENT_RUN_SSE_RETRY_MILLISECONDS = 1000
AGENT_RUN_SSE_HEARTBEAT_FRAME = b": heartbeat\n\n"
AGENT_RUN_SSE_RETRY_FRAME = b"retry: 1000\n\n"
_REDIS_STREAM_ID_PATTERN = re.compile(r"[0-9]+-[0-9]+")
_REDIS_STREAM_ID_PART_MAX = 2**64 - 1


class AgentRunLiveStreamReaderProtocol(Protocol):
    async def read_after(
        self,
        run_id: UUID,
        attempt_epoch: int,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult: ...


@dataclass(frozen=True, slots=True)
class AgentRunSseTiming:
    follow_interval: float = 0.5
    heartbeat_interval: float = 10.0
    connection_max_age: float = 45.0
    initial_stream_missing_grace: float = 2.0
    midstream_stream_missing_grace: float = 2.0
    repin_attempt_absent_grace: float = 2.0
    queued_recheck_interval: float = 2.0
    queued_wait_limit: float = 10.0


class AgentRunSsePreflightFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    CURSOR_TRIMMED = "cursor_trimmed"


def validate_redis_stream_id(stream_id: str) -> str:
    if _REDIS_STREAM_ID_PATTERN.fullmatch(stream_id) is None:
        raise ValueError("Invalid Redis Stream ID")
    milliseconds, sequence = stream_id.split("-", maxsplit=1)
    if (
        str(int(milliseconds)) != milliseconds
        or str(int(sequence)) != sequence
        or int(milliseconds) > _REDIS_STREAM_ID_PART_MAX
        or int(sequence) > _REDIS_STREAM_ID_PART_MAX
    ):
        raise ValueError("Invalid Redis Stream ID")
    return stream_id


def serialize_agent_run_sse_entry(entry: AgentRunLiveStreamEntry) -> bytes | None:
    event = entry.event
    data: dict[str, object] = {"attemptEpoch": entry.attempt_epoch}
    if isinstance(event, AgentRunLiveStreamAttemptStartedEvent):
        event_type = "attempt.started"
    elif isinstance(event, AgentRunLiveStreamStageEvent):
        event_type = "stage"
        data["stage"] = event.stage
    elif isinstance(event, AgentRunLiveStreamActivityEvent):
        event_type = "activity"
        data["activity"] = _camelize(event.activity.model_dump(mode="json"))
    elif isinstance(event, AgentRunLiveStreamAnswerDeltaEvent):
        event_type = "answer.delta"
        data["generation"] = event.generation
        data["text"] = event.text
    elif isinstance(event, AgentRunLiveStreamAnswerResetEvent):
        event_type = "answer.reset"
        data["generation"] = event.generation
    elif isinstance(event, AgentRunLiveStreamTerminalEvent):
        event_type = "terminal"
        data["status"] = event.status
        if event.error_code is not None:
            data["errorCode"] = event.error_code.value
    else:
        record_agent_run_sse_projection_drop()
        return None
    stream_id = validate_redis_stream_id(entry.stream_id)
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {stream_id}\nevent: {event_type}\ndata: {serialized}\n\n".encode()


def _camelize(value: Any) -> Any:
    if isinstance(value, dict):
        return {to_camel(str(key)): _camelize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


class AgentRunSseConnection:
    def __init__(
        self,
        *,
        run_id: UUID,
        attempt_epoch: int,
        cursor: str | None,
        reader: AgentRunLiveStreamReaderProtocol,
        lease: AgentRunSseCapacityLease,
        initial_result: AgentRunLiveStreamReadResult,
        timing: AgentRunSseTiming,
        started_at: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_disconnected: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._cursor = cursor
        self._reader = reader
        self._lease = lease
        self._initial_result = initial_result
        self._timing = timing
        self._started_at = started_at
        self._clock = clock
        self._sleep = sleep
        self._is_disconnected = is_disconnected

    async def frames(self) -> AsyncGenerator[bytes]:
        async for frame in self._frames(include_retry=True):
            yield frame

    async def _frames(self, *, include_retry: bool) -> AsyncGenerator[bytes]:
        last_sent_at = self._started_at
        pending_result: AgentRunLiveStreamReadResult | None = self._initial_result
        missing_since: float | None = None
        repinned_at: float | None = None
        try:
            await self._lease.mark_stream_started()
            if include_retry:
                yield AGENT_RUN_SSE_RETRY_FRAME
            while self._clock() - self._started_at < self._timing.connection_max_age:
                if self._is_disconnected is not None and await self._is_disconnected():
                    self._lease.mark_close_reason("client_disconnect")
                    return
                now = self._clock()
                if now - last_sent_at >= self._timing.heartbeat_interval:
                    yield AGENT_RUN_SSE_HEARTBEAT_FRAME
                    last_sent_at = now
                if pending_result is None:
                    pending_result = await self._reader.read_after(
                        self._run_id,
                        self._attempt_epoch,
                        self._cursor,
                    )
                result = pending_result
                pending_result = None
                if result.status is AgentRunLiveStreamReadStatus.EVENTS:
                    missing_since = None
                    repinned_at = None
                    if result.next_cursor is not None:
                        self._cursor = result.next_cursor
                    for entry in result.events:
                        frame = serialize_agent_run_sse_entry(entry)
                        if frame is None:
                            continue
                        yield frame
                        last_sent_at = self._clock()
                        if isinstance(entry.event, AgentRunLiveStreamTerminalEvent):
                            self._lease.mark_close_reason("terminal")
                            return
                    continue
                if result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED:
                    observed_epoch = result.observed_attempt_epoch
                    if observed_epoch is None or observed_epoch <= self._attempt_epoch:
                        self._lease.mark_close_reason("unavailable")
                        return
                    self._attempt_epoch = observed_epoch
                    if result.next_cursor is not None:
                        self._cursor = result.next_cursor
                    repinned_at = self._clock()
                    continue
                if result.next_cursor is not None:
                    self._cursor = result.next_cursor
                if result.status in (
                    AgentRunLiveStreamReadStatus.UNAVAILABLE,
                    AgentRunLiveStreamReadStatus.CURSOR_TRIMMED,
                ):
                    self._lease.mark_close_reason(
                        "cursor_trimmed"
                        if result.status is AgentRunLiveStreamReadStatus.CURSOR_TRIMMED
                        else "unavailable"
                    )
                    return
                if result.status is AgentRunLiveStreamReadStatus.STREAM_MISSING:
                    if missing_since is None:
                        missing_since = self._clock()
                    if (
                        self._clock() - missing_since
                        >= self._timing.midstream_stream_missing_grace
                    ):
                        self._lease.mark_close_reason("unavailable")
                        return
                else:
                    missing_since = None
                if (
                    result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
                    and repinned_at is not None
                    and self._clock() - repinned_at
                    >= self._timing.repin_attempt_absent_grace
                ):
                    self._lease.mark_close_reason("unavailable")
                    return
                remaining = self._timing.connection_max_age - (
                    self._clock() - self._started_at
                )
                if remaining <= 0:
                    self._lease.mark_close_reason("max_age")
                    return
                await self._sleep(min(self._timing.follow_interval, remaining))
            self._lease.mark_close_reason("max_age")
        except (asyncio.CancelledError, GeneratorExit):
            self._lease.mark_close_reason("client_disconnect")
            raise
        except Exception:
            self._lease.mark_close_reason("unavailable")
            raise
        finally:
            await self._lease.release()


class AgentRunQueuedSseConnection:
    def __init__(
        self,
        *,
        run_id: UUID,
        cursor: str | None,
        reader: AgentRunLiveStreamReaderProtocol,
        lease: AgentRunSseCapacityLease,
        load_context: Callable[[], Awaitable[OwnedAgentRunLiveContext | None]],
        timing: AgentRunSseTiming,
        started_at: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_disconnected: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        self._run_id = run_id
        self._cursor = cursor
        self._reader = reader
        self._lease = lease
        self._load_context = load_context
        self._timing = timing
        self._started_at = started_at
        self._clock = clock
        self._sleep = sleep
        self._is_disconnected = is_disconnected

    async def frames(self) -> AsyncGenerator[bytes]:
        self._lease.mark_close_reason("queued_timeout")
        queued_started_at = self._clock()
        last_sent_at = self._started_at
        try:
            await self._lease.mark_stream_started()
            yield AGENT_RUN_SSE_RETRY_FRAME
            while (
                self._clock() - queued_started_at < self._timing.queued_wait_limit
                and self._clock() - self._started_at < self._timing.connection_max_age
            ):
                if self._is_disconnected is not None and await self._is_disconnected():
                    self._lease.mark_close_reason("client_disconnect")
                    return
                remaining = min(
                    self._timing.queued_recheck_interval,
                    self._timing.queued_wait_limit
                    - (self._clock() - queued_started_at),
                    self._timing.connection_max_age
                    - (self._clock() - self._started_at),
                )
                if remaining <= 0:
                    return
                await self._sleep(remaining)
                if self._clock() - last_sent_at >= self._timing.heartbeat_interval:
                    yield AGENT_RUN_SSE_HEARTBEAT_FRAME
                    last_sent_at = self._clock()
                try:
                    context = await self._load_context()
                except Exception:
                    self._lease.mark_close_reason("unavailable")
                    return
                if context is None or context.status in (
                    AgentRunStatus.COMPLETED,
                    AgentRunStatus.POLICY_BLOCKED,
                    AgentRunStatus.FAILED,
                ):
                    self._lease.mark_close_reason("queued_terminal")
                    return
                if context.attempt_epoch < 1:
                    continue
                result = await self._reader.read_after(
                    self._run_id,
                    context.attempt_epoch,
                    self._cursor,
                )
                connection = AgentRunSseConnection(
                    run_id=self._run_id,
                    attempt_epoch=context.attempt_epoch,
                    cursor=self._cursor,
                    reader=self._reader,
                    lease=self._lease,
                    initial_result=result,
                    timing=self._timing,
                    started_at=self._started_at,
                    clock=self._clock,
                    sleep=self._sleep,
                    is_disconnected=self._is_disconnected,
                )
                async for frame in connection._frames(include_retry=False):
                    yield frame
                return
            if self._clock() - self._started_at >= self._timing.connection_max_age:
                self._lease.mark_close_reason("max_age")
        except (asyncio.CancelledError, GeneratorExit):
            self._lease.mark_close_reason("client_disconnect")
            raise
        except Exception:
            self._lease.mark_close_reason("unavailable")
            raise
        finally:
            await self._lease.release()


async def prepare_running_sse_connection(
    *,
    run_id: UUID,
    attempt_epoch: int,
    cursor: str | None,
    reader: AgentRunLiveStreamReaderProtocol,
    lease: AgentRunSseCapacityLease,
    timing: AgentRunSseTiming,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    started_at: float | None = None,
) -> AgentRunSseConnection | AgentRunSsePreflightFailure:
    connection_started_at = clock() if started_at is None else started_at
    missing_since: float | None = None
    pinned_epoch = attempt_epoch
    current_cursor = cursor
    while True:
        result = await reader.read_after(run_id, pinned_epoch, current_cursor)
        if result.status is AgentRunLiveStreamReadStatus.UNAVAILABLE:
            await lease.release()
            return AgentRunSsePreflightFailure.UNAVAILABLE
        if result.status is AgentRunLiveStreamReadStatus.CURSOR_TRIMMED:
            await lease.release()
            return AgentRunSsePreflightFailure.CURSOR_TRIMMED
        if result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED:
            observed_epoch = result.observed_attempt_epoch
            if observed_epoch is None or observed_epoch <= pinned_epoch:
                await lease.release()
                return AgentRunSsePreflightFailure.UNAVAILABLE
            pinned_epoch = observed_epoch
            if result.next_cursor is not None:
                current_cursor = result.next_cursor
            continue
        if result.status is not AgentRunLiveStreamReadStatus.STREAM_MISSING:
            return AgentRunSseConnection(
                run_id=run_id,
                attempt_epoch=pinned_epoch,
                cursor=current_cursor,
                reader=reader,
                lease=lease,
                initial_result=result,
                timing=timing,
                started_at=connection_started_at,
                clock=clock,
                sleep=sleep,
                is_disconnected=is_disconnected,
            )
        if missing_since is None:
            missing_since = clock()
        if clock() - missing_since >= timing.initial_stream_missing_grace:
            await lease.release()
            return AgentRunSsePreflightFailure.UNAVAILABLE
        remaining = timing.initial_stream_missing_grace - (clock() - missing_since)
        await sleep(min(timing.follow_interval, remaining))


class AgentRunSseCapacityRejection(StrEnum):
    RUN = "run"
    USER = "user"


class AgentRunSseCapacityLease:
    def __init__(
        self,
        capacity: AgentRunSseCapacity,
        *,
        process_acquired_at: float,
    ) -> None:
        self._capacity = capacity
        self._process_acquired_at = process_acquired_at
        self._run_id: UUID | None = None
        self._user_id: UUID | None = None
        self._released = False
        self._stream_started_at: float | None = None
        self._close_reason: AgentRunSseCloseReason = "unavailable"

    async def try_acquire_owned(
        self,
        *,
        run_id: UUID,
        user_id: UUID,
    ) -> AgentRunSseCapacityRejection | None:
        return await self._capacity._try_acquire_owned(  # noqa: SLF001
            self,
            run_id=run_id,
            user_id=user_id,
        )

    async def release(self) -> None:
        await self._capacity._release(self)  # noqa: SLF001

    async def mark_stream_started(self) -> None:
        await self._capacity._mark_stream_started(self)  # noqa: SLF001

    def mark_close_reason(self, reason: AgentRunSseCloseReason) -> None:
        self._close_reason = reason

    async def __aenter__(self) -> AgentRunSseCapacityLease:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.release()


class AgentRunSseCapacity:
    def __init__(
        self,
        *,
        run_limit: int = AGENT_RUN_SSE_RUN_CONNECTION_LIMIT,
        user_limit: int = AGENT_RUN_SSE_USER_CONNECTION_LIMIT,
        process_limit: int = AGENT_RUN_SSE_PROCESS_CONNECTION_LIMIT,
        lease_ttl_seconds: float = AGENT_RUN_SSE_LEASE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(run_limit, user_limit, process_limit) < 1 or lease_ttl_seconds <= 0:
            raise ValueError("SSE capacity limits must be positive")
        self._run_limit = run_limit
        self._user_limit = user_limit
        self._process_limit = process_limit
        self._lease_ttl_seconds = lease_ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active_process = 0
        self._active_by_run: dict[UUID, int] = {}
        self._active_by_user: dict[UUID, int] = {}
        self._leases: set[AgentRunSseCapacityLease] = set()

    async def try_acquire_process(self) -> AgentRunSseCapacityLease | None:
        async with self._lock:
            self._prune_expired_locked()
            if self._active_process >= self._process_limit:
                record_agent_run_sse_capacity_rejection(scope="process")
                return None
            self._active_process += 1
            lease = AgentRunSseCapacityLease(
                self,
                process_acquired_at=self._clock(),
            )
            self._leases.add(lease)
            return lease

    async def _try_acquire_owned(
        self,
        lease: AgentRunSseCapacityLease,
        *,
        run_id: UUID,
        user_id: UUID,
    ) -> AgentRunSseCapacityRejection | None:
        async with self._lock:
            if lease._released:  # noqa: SLF001
                raise RuntimeError("SSE capacity lease is already released")
            if lease._run_id is not None:  # noqa: SLF001
                raise RuntimeError("SSE owned capacity is already acquired")
            if self._active_by_run.get(run_id, 0) >= self._run_limit:
                record_agent_run_sse_capacity_rejection(scope="run")
                self._release_process_locked(lease)
                return AgentRunSseCapacityRejection.RUN
            if self._active_by_user.get(user_id, 0) >= self._user_limit:
                record_agent_run_sse_capacity_rejection(scope="user")
                self._release_process_locked(lease)
                return AgentRunSseCapacityRejection.USER
            self._active_by_run[run_id] = self._active_by_run.get(run_id, 0) + 1
            self._active_by_user[user_id] = self._active_by_user.get(user_id, 0) + 1
            lease._run_id = run_id  # noqa: SLF001
            lease._user_id = user_id  # noqa: SLF001
            return None

    async def _mark_stream_started(self, lease: AgentRunSseCapacityLease) -> None:
        async with self._lock:
            if lease._released:  # noqa: SLF001
                raise RuntimeError("SSE capacity lease is already released")
            if lease._stream_started_at is not None:  # noqa: SLF001
                return
            lease._stream_started_at = self._clock()  # noqa: SLF001
            record_agent_run_sse_open()

    async def _release(self, lease: AgentRunSseCapacityLease) -> None:
        async with self._lock:
            self._release_locked(lease)

    def _release_locked(self, lease: AgentRunSseCapacityLease) -> None:
        if lease._released:  # noqa: SLF001
            return
        run_id = lease._run_id  # noqa: SLF001
        user_id = lease._user_id  # noqa: SLF001
        if run_id is not None and user_id is not None:
            self._decrement(self._active_by_run, run_id)
            self._decrement(self._active_by_user, user_id)
            started_at = lease._stream_started_at  # noqa: SLF001
            if started_at is not None:
                record_agent_run_sse_close(
                    duration_seconds=max(0.0, self._clock() - started_at),
                    reason=lease._close_reason,  # noqa: SLF001
                )
        self._release_process_locked(lease)

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for lease in tuple(self._leases):
            if now - lease._process_acquired_at < self._lease_ttl_seconds:  # noqa: SLF001
                continue
            lease.mark_close_reason("lease_expired")
            self._release_locked(lease)

    def _release_process_locked(self, lease: AgentRunSseCapacityLease) -> None:
        if lease._released:  # noqa: SLF001
            return
        if self._active_process < 1:
            raise RuntimeError("SSE process capacity underflow")
        self._active_process -= 1
        lease._released = True  # noqa: SLF001
        self._leases.discard(lease)

    @staticmethod
    def _decrement(counts: dict[UUID, int], key: UUID) -> None:
        remaining = counts[key] - 1
        if remaining == 0:
            del counts[key]
        else:
            counts[key] = remaining
