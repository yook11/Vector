"""Agent run live Redis event tests."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from app.agent.contract import (
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchEvidenceSelectedEvent,
    ExternalSearchQueriesGeneratedEvent,
    InternalSearchCompletedEvent,
    InternalSearchStartedEvent,
    QuestionResolvedEvent,
)
from app.agent.live_updates.recent_events import (
    AGENT_RUN_LIVE_EVENT_READ_LIMIT,
    AGENT_RUN_LIVE_EVENT_TTL_SECONDS,
    AgentRunLiveEventPublisher,
    AgentRunLiveEventReader,
    agent_run_live_events_key,
)
from app.config import settings

pytestmark = pytest.mark.xdist_group("redis")

RUN_ID = UUID("00000000-0000-4000-a000-000000000010")


class StaticRedis:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        _ = key
        assert start == 0
        assert end == AGENT_RUN_LIVE_EVENT_READ_LIMIT - 1
        return self.values


class RaisingRedis:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def pipeline(self) -> object:
        raise self.exc

    async def lrange(self, *_args: object) -> list[str]:
        raise self.exc

    async def delete(self, *_args: object) -> int:
        raise self.exc


class HangingPipeline:
    def lpush(self, *_args: object) -> HangingPipeline:
        return self

    def ltrim(self, *_args: object) -> HangingPipeline:
        return self

    def expire(self, *_args: object) -> HangingPipeline:
        return self

    async def execute(self) -> None:
        await _sleep_forever()


class HangingRedis:
    def pipeline(self) -> HangingPipeline:
        return HangingPipeline()

    async def lrange(self, *_args: object) -> list[str]:
        await _sleep_forever()

    async def delete(self, *_args: object) -> int:
        await _sleep_forever()


async def _sleep_forever() -> None:
    while True:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_publisher_reader_round_trip_uses_real_redis_semantics() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    key = agent_run_live_events_key(RUN_ID)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveEventPublisher(redis, RUN_ID)
        reader = AgentRunLiveEventReader(redis)

        for index in range(51):
            await publisher.event_occurred(
                ExternalSearchQueriesGeneratedEvent(
                    task_index=index,
                    queries=[f"NVIDIA AI query {index}"],
                )
            )

        stored = await redis.lrange(key, 0, -1)
        assert len(stored) == 50
        newest = json.loads(stored[0])
        oldest = json.loads(stored[-1])
        assert newest["task_index"] == 50
        assert oldest["task_index"] == 1
        assert await redis.ttl(key) in range(1, AGENT_RUN_LIVE_EVENT_TTL_SECONDS + 1)

        events = await reader.recent_events(RUN_ID)

        assert [event.task_index for event in events] == list(range(41, 51))
        assert events[0].type == "external_search.queries_generated"
        assert events[0].queries == ["NVIDIA AI query 41"]
        assert events[-1].queries == ["NVIDIA AI query 50"]

        await publisher.reset()

        assert await redis.exists(key) == 0
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_all_contract_event_types_round_trip_through_api_schema() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveEventPublisher(redis, RUN_ID)
        reader = AgentRunLiveEventReader(redis)
        events = [
            InternalSearchStartedEvent(query_count=2),
            InternalSearchCompletedEvent(hit_count=3),
            ExternalSearchQueriesGeneratedEvent(
                task_index=0,
                queries=["NVIDIA AI"],
            ),
            ExternalSearchCandidatesFetchedEvent(
                task_index=0,
                candidate_count=8,
            ),
            ExternalSearchEvidenceSelectedEvent(
                task_index=0,
                evidence_count=2,
            ),
            QuestionResolvedEvent(
                standalone_question="NVIDIA の発表が株価へ与える影響は？"
            ),
        ]

        for event in events:
            await publisher.event_occurred(event)

        recent_events = await reader.recent_events(RUN_ID)

        assert [event.type for event in recent_events] == [
            "internal_search.started",
            "internal_search.completed",
            "external_search.queries_generated",
            "external_search.candidates_fetched",
            "external_search.evidence_selected",
            "question.resolved",
        ]
        assert recent_events[0].query_count == 2
        assert recent_events[1].hit_count == 3
        assert recent_events[2].queries == ["NVIDIA AI"]
        assert recent_events[3].candidate_count == 8
        assert recent_events[4].evidence_count == 2
        assert recent_events[5].standalone_question == (
            "NVIDIA の発表が株価へ与える影響は？"
        )
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
async def test_publisher_failure_logs_without_event_payload() -> None:
    redis = RaisingRedis(RedisConnectionError("redis down SECRET_QUERY"))
    publisher = AgentRunLiveEventPublisher(redis, RUN_ID)

    with capture_logs() as logs:
        await publisher.event_occurred(
            ExternalSearchQueriesGeneratedEvent(
                task_index=0,
                queries=["SECRET_QUERY"],
            )
        )

    assert logs[0]["event"] == "agent_run_live_event_publish_failed"
    assert logs[0]["run_id"] == str(RUN_ID)
    assert logs[0]["event_type"] == "external_search.queries_generated"
    assert "SECRET_QUERY" not in repr(logs)
    assert "redis down" not in repr(logs)


@pytest.mark.asyncio
async def test_reader_returns_oldest_first_and_skips_bad_entries() -> None:
    older = {
        "type": "internal_search.completed",
        "ts": "2026-07-09T01:00:00+00:00",
        "hit_count": 4,
    }
    newer = {
        "type": "external_search.queries_generated",
        "ts": "2026-07-09T01:00:02+00:00",
        "task_index": 0,
        "queries": ["NVIDIA AI"],
    }
    unknown = {
        "type": "future.event",
        "ts": "2026-07-09T01:00:01+00:00",
    }
    redis = StaticRedis(
        [
            json.dumps(newer),
            "not-json",
            json.dumps(unknown),
            json.dumps(older),
        ]
    )
    reader = AgentRunLiveEventReader(redis)

    events = await reader.recent_events(RUN_ID)

    assert [event.type for event in events] == [
        "internal_search.completed",
        "external_search.queries_generated",
    ]
    assert events[0].ts == datetime(2026, 7, 9, 1, 0, tzinfo=UTC)
    assert events[1].task_index == 0
    assert events[1].queries == ["NVIDIA AI"]


@pytest.mark.asyncio
async def test_reader_failure_returns_empty_without_payload_log() -> None:
    redis = RaisingRedis(RedisConnectionError("redis down SECRET_QUERY"))
    reader = AgentRunLiveEventReader(redis)

    with capture_logs() as logs:
        events = await reader.recent_events(RUN_ID)

    assert events == []
    assert logs[0]["event"] == "agent_run_live_event_read_failed"
    assert logs[0]["run_id"] == str(RUN_ID)
    assert "SECRET_QUERY" not in repr(logs)
    assert "redis down" not in repr(logs)


@pytest.mark.asyncio
async def test_publisher_reset_and_reader_timeout_quickly() -> None:
    redis = HangingRedis()
    publisher = AgentRunLiveEventPublisher(redis, RUN_ID, timeout_seconds=0.02)
    reader = AgentRunLiveEventReader(redis, timeout_seconds=0.02)

    start = time.monotonic()
    with capture_logs() as logs:
        await publisher.event_occurred(
            ExternalSearchQueriesGeneratedEvent(
                task_index=0,
                queries=["SECRET_QUERY"],
            )
        )
        await publisher.reset()
        events = await reader.recent_events(RUN_ID)
    elapsed = time.monotonic() - start

    assert events == []
    assert elapsed < 0.5
    assert [entry["event"] for entry in logs] == [
        "agent_run_live_event_publish_failed",
        "agent_run_live_event_reset_failed",
        "agent_run_live_event_read_failed",
    ]
    assert "SECRET_QUERY" not in repr(logs)


def test_read_limit_is_smaller_than_storage_cap() -> None:
    assert AGENT_RUN_LIVE_EVENT_READ_LIMIT == 10
