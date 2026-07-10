"""Best-effort Redis Stream transport for active agent-run live updates."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

import redis.asyncio as aioredis
import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agent.contract import AnswerProgressEvent
from app.agent.runs.types import AgentRunErrorCode

AGENT_RUN_LIVE_STREAM_MAXLEN = 4096
AGENT_RUN_LIVE_STREAM_PAGE_SIZE = 128
AGENT_RUN_LIVE_STREAM_TTL_SECONDS = 15 * 60
AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS = 0.5

logger = structlog.get_logger(__name__)


class _StreamEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentRunLiveStreamAttemptStartedEvent(_StreamEventBase):
    type: Literal["attempt.started"] = "attempt.started"


class AgentRunLiveStreamStageEvent(_StreamEventBase):
    type: Literal["stage"] = "stage"
    stage: Literal["planning", "retrieving", "synthesizing"]


class AgentRunLiveStreamActivityEvent(_StreamEventBase):
    type: Literal["activity"] = "activity"
    event: AnswerProgressEvent


class AgentRunLiveStreamAnswerDeltaEvent(_StreamEventBase):
    type: Literal["answer.delta"] = "answer.delta"
    generation: int = Field(ge=1)
    text: str = Field(min_length=1)


class AgentRunLiveStreamAnswerResetEvent(_StreamEventBase):
    type: Literal["answer.reset"] = "answer.reset"
    generation: int = Field(ge=1)


class AgentRunLiveStreamTerminalEvent(_StreamEventBase):
    type: Literal["terminal"] = "terminal"
    status: Literal["completed", "failed"]
    error_code: AgentRunErrorCode | None = Field(default=None, alias="errorCode")


AgentRunLiveStreamEvent = Annotated[
    AgentRunLiveStreamAttemptStartedEvent
    | AgentRunLiveStreamStageEvent
    | AgentRunLiveStreamActivityEvent
    | AgentRunLiveStreamAnswerDeltaEvent
    | AgentRunLiveStreamAnswerResetEvent
    | AgentRunLiveStreamTerminalEvent,
    Field(discriminator="type"),
]
_event_adapter = TypeAdapter(AgentRunLiveStreamEvent)


class _StreamEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: str
    attempt_epoch: datetime = Field(alias="attemptEpoch")
    payload: str
    published_at: datetime = Field(alias="publishedAt")


class AgentRunLiveStreamReadStatus(StrEnum):
    EVENTS = "events"
    EMPTY = "empty"
    STREAM_MISSING = "stream_missing"
    ATTEMPT_ABSENT = "attempt_absent"
    CURSOR_TRIMMED = "cursor_trimmed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AgentRunLiveStreamEntry:
    stream_id: str
    attempt_epoch: datetime
    event: AgentRunLiveStreamEvent


@dataclass(frozen=True, slots=True)
class AgentRunLiveStreamReadResult:
    status: AgentRunLiveStreamReadStatus
    events: tuple[AgentRunLiveStreamEntry, ...] = ()
    next_cursor: str | None = None


def agent_run_live_stream_key(run_id: UUID) -> str:
    return f"agent:run:{run_id}:live"


def is_stream_id_before(left: str, right: str) -> bool:
    return _stream_id_parts(left) < _stream_id_parts(right)


class AgentRunLiveStreamPublisher:
    def __init__(
        self,
        redis: aioredis.Redis,
        run_id: UUID,
        attempt_epoch: datetime,
        *,
        timeout_seconds: float = AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._key = agent_run_live_stream_key(run_id)
        self._timeout_seconds = timeout_seconds
        self._attempt_started_confirmed = False

    async def begin_attempt(self) -> str | None:
        if self._attempt_started_confirmed:
            return None
        marker = AgentRunLiveStreamAttemptStartedEvent()
        try:
            stream_ids = await asyncio.wait_for(
                self._append((marker,)),
                timeout=self._timeout_seconds,
            )
        except Exception:
            self._log_publish_failure("begin_attempt", marker.type)
            return None
        self._attempt_started_confirmed = True
        return stream_ids[-1]

    async def publish(self, event: AgentRunLiveStreamEvent) -> str | None:
        events: tuple[AgentRunLiveStreamEvent, ...]
        marker_added = not self._attempt_started_confirmed
        if marker_added:
            events = (AgentRunLiveStreamAttemptStartedEvent(), event)
        else:
            events = (event,)
        try:
            stream_ids = await asyncio.wait_for(
                self._append(events),
                timeout=self._timeout_seconds,
            )
        except Exception:
            self._log_publish_failure("publish", event.type)
            return None
        if marker_added:
            self._attempt_started_confirmed = True
        return stream_ids[-1]

    async def _append(
        self,
        events: tuple[AgentRunLiveStreamEvent, ...],
    ) -> list[str]:
        pipeline = self._redis.pipeline()
        for event in events:
            pipeline.xadd(
                self._key,
                _encode_envelope(event, self._attempt_epoch),
                maxlen=AGENT_RUN_LIVE_STREAM_MAXLEN,
                approximate=False,
            )
        pipeline.expire(self._key, AGENT_RUN_LIVE_STREAM_TTL_SECONDS)
        replies = await pipeline.execute()
        return [_as_stream_id(reply) for reply in replies[: len(events)]]

    def _log_publish_failure(self, operation: str, event_type: str) -> None:
        logger.warning(
            "agent_run_live_stream_publish_failed",
            run_id=str(self._run_id),
            operation=operation,
            event_type=event_type,
        )


class AgentRunLiveStreamReader:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        timeout_seconds: float = AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._timeout_seconds = timeout_seconds

    async def read_after(
        self,
        run_id: UUID,
        attempt_epoch: datetime,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult:
        try:
            return await asyncio.wait_for(
                self._read_after(run_id, attempt_epoch, cursor),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "agent_run_live_stream_read_failed",
                run_id=str(run_id),
            )
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.UNAVAILABLE
            )

    async def _read_after(
        self,
        run_id: UUID,
        attempt_epoch: datetime,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult:
        key = agent_run_live_stream_key(run_id)
        if cursor is None:
            raw_entries = await self._redis.xrange(
                key,
                "-",
                "+",
                count=AGENT_RUN_LIVE_STREAM_MAXLEN,
            )
            if not raw_entries:
                return await self._empty_initial_result(key)
            return self._initial_result(raw_entries, attempt_epoch)

        earliest = await self._redis.xrange(key, "-", "+", count=1)
        if not earliest:
            if not await self._redis.exists(key):
                return AgentRunLiveStreamReadResult(
                    status=AgentRunLiveStreamReadStatus.STREAM_MISSING
                )
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.EMPTY,
                next_cursor=cursor,
            )
        earliest_id = _as_stream_id(earliest[0][0])
        if is_stream_id_before(cursor, earliest_id):
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.CURSOR_TRIMMED
            )

        response = await self._redis.xread(
            {key: cursor}, count=AGENT_RUN_LIVE_STREAM_PAGE_SIZE
        )
        raw_entries = _xread_entries(response)
        if not raw_entries:
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.EMPTY,
                next_cursor=cursor,
            )
        next_cursor = _as_stream_id(raw_entries[-1][0])
        events = _decode_entries(raw_entries, attempt_epoch)
        if not events:
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.EMPTY,
                next_cursor=next_cursor,
            )
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=tuple(events),
            next_cursor=next_cursor,
        )

    async def _empty_initial_result(
        self,
        key: str,
    ) -> AgentRunLiveStreamReadResult:
        if not await self._redis.exists(key):
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.STREAM_MISSING
            )
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
        )

    def _initial_result(
        self,
        raw_entries: list[tuple[Any, Any]],
        attempt_epoch: datetime,
    ) -> AgentRunLiveStreamReadResult:
        events: list[AgentRunLiveStreamEntry] = []
        next_cursor: str | None = None
        for stream_id, fields in raw_entries:
            next_cursor = _as_stream_id(stream_id)
            entry = _decode_entry(stream_id, fields)
            if entry is not None and entry.attempt_epoch == attempt_epoch:
                events.append(entry)
            if len(events) == AGENT_RUN_LIVE_STREAM_PAGE_SIZE:
                break
        if not events:
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
            )
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=tuple(events),
            next_cursor=next_cursor,
        )


def _encode_envelope(
    event: AgentRunLiveStreamEvent,
    attempt_epoch: datetime,
) -> dict[str, str]:
    payload = event.model_dump(mode="json", by_alias=True)
    event_type = payload.pop("type")
    return {
        "type": event_type,
        "attemptEpoch": attempt_epoch.astimezone(UTC).isoformat(),
        "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "publishedAt": datetime.now(UTC).isoformat(),
    }


def _decode_entries(
    raw_entries: list[tuple[Any, Any]],
    attempt_epoch: datetime,
) -> list[AgentRunLiveStreamEntry]:
    events: list[AgentRunLiveStreamEntry] = []
    for stream_id, fields in raw_entries:
        entry = _decode_entry(stream_id, fields)
        if entry is not None and entry.attempt_epoch == attempt_epoch:
            events.append(entry)
    return events


def _decode_entry(stream_id: Any, fields: Any) -> AgentRunLiveStreamEntry | None:
    try:
        envelope = _StreamEnvelope.model_validate(_string_fields(fields))
        payload = json.loads(envelope.payload)
        if not isinstance(payload, dict):
            return None
        payload["type"] = envelope.type
        event = _event_adapter.validate_python(payload)
        return AgentRunLiveStreamEntry(
            stream_id=_as_stream_id(stream_id),
            attempt_epoch=envelope.attempt_epoch,
            event=event,
        )
    except (
        TypeError,
        ValueError,
        ValidationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None


def _string_fields(fields: Any) -> dict[str, str]:
    if not isinstance(fields, dict):
        raise TypeError("Stream fields must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in fields.items():
        normalized[_as_text(key)] = _as_text(value)
    return normalized


def _xread_entries(response: Any) -> list[tuple[Any, Any]]:
    if not response:
        return []
    entries: list[tuple[Any, Any]] = []
    for _key, stream_entries in response:
        entries.extend(stream_entries)
    return entries


def _as_stream_id(value: Any) -> str:
    return _as_text(value)


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("Redis Stream field must be text")


def _stream_id_parts(stream_id: str) -> tuple[int, int]:
    milliseconds, separator, sequence = stream_id.partition("-")
    if not separator or not milliseconds or not sequence:
        raise ValueError("Invalid Redis Stream ID")
    return int(milliseconds), int(sequence)
