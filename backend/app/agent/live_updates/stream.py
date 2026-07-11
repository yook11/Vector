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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

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
    activity: AnswerProgressEvent


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
    attempt_epoch: int = Field(ge=1, alias="attemptEpoch")
    payload: str
    published_at: str = Field(alias="publishedAt")

    @field_validator("attempt_epoch", mode="before")
    @classmethod
    def parse_attempt_epoch(cls, value: object) -> int:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError("attempt epoch must be a decimal integer string")
        return int(value)


class AgentRunLiveStreamReadStatus(StrEnum):
    EVENTS = "events"
    EMPTY = "empty"
    STREAM_MISSING = "stream_missing"
    ATTEMPT_ABSENT = "attempt_absent"
    ATTEMPT_ADVANCED = "attempt_advanced"
    CURSOR_TRIMMED = "cursor_trimmed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AgentRunLiveStreamEntry:
    stream_id: str
    attempt_epoch: int
    event: AgentRunLiveStreamEvent


@dataclass(frozen=True, slots=True)
class AgentRunLiveStreamReadResult:
    status: AgentRunLiveStreamReadStatus
    events: tuple[AgentRunLiveStreamEntry, ...] = ()
    next_cursor: str | None = None
    observed_attempt_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED:
            if self.observed_attempt_epoch is None or self.observed_attempt_epoch < 1:
                raise ValueError(
                    "observed attempt epoch is required for attempt advanced"
                )
            if self.events:
                raise ValueError("attempt advanced cannot contain events")
        elif self.observed_attempt_epoch is not None:
            raise ValueError(
                "observed attempt epoch is only valid for attempt advanced"
            )


@dataclass(frozen=True, slots=True)
class _DecodedStreamEnvelope:
    stream_id: str
    attempt_epoch: int
    event_type: str
    payload: str


def agent_run_live_stream_key(run_id: UUID) -> str:
    return f"agent:run:{run_id}:live"


def is_stream_id_before(left: str, right: str) -> bool:
    return _stream_id_parts(left) < _stream_id_parts(right)


class AgentRunLiveStreamPublisher:
    def __init__(
        self,
        redis: aioredis.Redis,
        run_id: UUID,
        attempt_epoch: int,
        *,
        timeout_seconds: float = AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
    ) -> None:
        _validate_attempt_epoch(attempt_epoch)
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
        attempt_epoch: int,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult:
        _validate_attempt_epoch(attempt_epoch)
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
        attempt_epoch: int,
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
            return _entries_result(raw_entries, attempt_epoch, previous_cursor=None)

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
        return _entries_result(
            raw_entries,
            attempt_epoch,
            previous_cursor=cursor,
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


def _encode_envelope(
    event: AgentRunLiveStreamEvent,
    attempt_epoch: int,
) -> dict[str, str]:
    payload = event.model_dump(mode="json", by_alias=True)
    event_type = payload.pop("type")
    return {
        "type": event_type,
        "attemptEpoch": str(attempt_epoch),
        "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "publishedAt": datetime.now(UTC).isoformat(),
    }


def _entries_result(
    raw_entries: list[tuple[Any, Any]],
    attempt_epoch: int,
    *,
    previous_cursor: str | None,
) -> AgentRunLiveStreamReadResult:
    events: list[AgentRunLiveStreamEntry] = []
    next_cursor = previous_cursor
    for stream_id, fields in raw_entries:
        stream_id_text = _as_stream_id(stream_id)
        entry = _decode_envelope(stream_id, fields)
        if entry is None:
            next_cursor = stream_id_text
            continue
        if entry.attempt_epoch < attempt_epoch:
            next_cursor = stream_id_text
            continue
        if entry.attempt_epoch > attempt_epoch:
            if events:
                break
            return AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
                next_cursor=next_cursor,
                observed_attempt_epoch=entry.attempt_epoch,
            )
        next_cursor = stream_id_text
        event = _decode_event(entry)
        if event is not None:
            events.append(
                AgentRunLiveStreamEntry(
                    stream_id=entry.stream_id,
                    attempt_epoch=entry.attempt_epoch,
                    event=event,
                )
            )
        if len(events) == AGENT_RUN_LIVE_STREAM_PAGE_SIZE:
            break
    if events:
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=tuple(events),
            next_cursor=next_cursor,
        )
    return AgentRunLiveStreamReadResult(
        status=AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT,
        next_cursor=next_cursor,
    )


def _decode_envelope(
    stream_id: Any,
    fields: Any,
) -> _DecodedStreamEnvelope | None:
    try:
        envelope = _StreamEnvelope.model_validate(_string_fields(fields))
        stream_id_text = _as_stream_id(stream_id)
    except (TypeError, ValueError, ValidationError, UnicodeDecodeError):
        return None
    return _DecodedStreamEnvelope(
        stream_id=stream_id_text,
        attempt_epoch=envelope.attempt_epoch,
        event_type=envelope.type,
        payload=envelope.payload,
    )


def _decode_event(entry: _DecodedStreamEnvelope) -> AgentRunLiveStreamEvent | None:
    try:
        payload = json.loads(entry.payload)
        if isinstance(payload, dict):
            payload["type"] = entry.event_type
            return _event_adapter.validate_python(payload)
    except (
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ):
        pass
    return None


def _validate_attempt_epoch(attempt_epoch: int) -> None:
    if (
        isinstance(attempt_epoch, bool)
        or not isinstance(attempt_epoch, int)
        or attempt_epoch < 1
    ):
        raise ValueError("attempt epoch must be positive")


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
