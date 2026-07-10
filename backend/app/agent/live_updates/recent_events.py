"""Best-effort Redis recent events for active agent runs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
import structlog
from pydantic import TypeAdapter, ValidationError

from app.agent.contract import AnswerProgressEvent
from app.schemas.research import ResearchRunEvent

AGENT_RUN_LIVE_EVENT_LIST_LIMIT = 50
AGENT_RUN_LIVE_EVENT_READ_LIMIT = 10
AGENT_RUN_LIVE_EVENT_TTL_SECONDS = 15 * 60
AGENT_RUN_LIVE_EVENT_TIMEOUT_SECONDS = 0.5

logger = structlog.get_logger(__name__)
_event_adapter = TypeAdapter(ResearchRunEvent)


def agent_run_live_events_key(run_id: UUID) -> str:
    return f"agent:run:{run_id}:events"


class AgentRunLiveEventPublisher:
    def __init__(
        self,
        redis: aioredis.Redis,
        run_id: UUID,
        *,
        timeout_seconds: float = AGENT_RUN_LIVE_EVENT_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._run_id = run_id
        self._key = agent_run_live_events_key(run_id)
        self._timeout_seconds = timeout_seconds

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        event_type = getattr(event, "type", "unknown")
        try:
            payload = event.model_dump(mode="json")
            payload["ts"] = datetime.now(UTC).isoformat()
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            await asyncio.wait_for(
                self._push(encoded),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "agent_run_live_event_publish_failed",
                run_id=str(self._run_id),
                event_type=event_type,
            )

    async def reset(self) -> None:
        try:
            await asyncio.wait_for(
                self._redis.delete(self._key),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "agent_run_live_event_reset_failed",
                run_id=str(self._run_id),
            )

    async def _push(self, encoded: str) -> None:
        pipeline = self._redis.pipeline()
        pipeline.lpush(self._key, encoded)
        pipeline.ltrim(self._key, 0, AGENT_RUN_LIVE_EVENT_LIST_LIMIT - 1)
        pipeline.expire(self._key, AGENT_RUN_LIVE_EVENT_TTL_SECONDS)
        await pipeline.execute()


class AgentRunLiveEventReader:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        timeout_seconds: float = AGENT_RUN_LIVE_EVENT_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._timeout_seconds = timeout_seconds

    async def recent_events(self, run_id: UUID) -> list[ResearchRunEvent]:
        key = agent_run_live_events_key(run_id)
        try:
            entries = await asyncio.wait_for(
                self._redis.lrange(key, 0, AGENT_RUN_LIVE_EVENT_READ_LIMIT - 1),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "agent_run_live_event_read_failed",
                run_id=str(run_id),
            )
            return []

        events: list[ResearchRunEvent] = []
        for entry in reversed(entries):
            event = _decode_event(entry)
            if event is not None:
                events.append(event)
        return events


def _decode_event(entry: Any) -> ResearchRunEvent | None:
    try:
        if isinstance(entry, bytes):
            entry = entry.decode("utf-8")
        payload = json.loads(entry)
        return _event_adapter.validate_python(payload)
    except (TypeError, ValueError, ValidationError, UnicodeDecodeError):
        return None
