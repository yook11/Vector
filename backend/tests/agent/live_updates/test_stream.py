"""Redis Stream transport tests for active agent runs."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from app.agent.contract import InternalSearchStartedEvent
from app.agent.live_updates.stream import (
    AGENT_RUN_LIVE_STREAM_MAXLEN,
    AGENT_RUN_LIVE_STREAM_PAGE_SIZE,
    AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
    AGENT_RUN_LIVE_STREAM_TTL_SECONDS,
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamReader,
    AgentRunLiveStreamReadResult,
    AgentRunLiveStreamReadStatus,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
    agent_run_live_stream_key,
    is_stream_id_before,
)
from app.config import settings

pytestmark = pytest.mark.xdist_group("redis")

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
EPOCH_1 = 1
EPOCH_2 = 2
EPOCH_3 = 3
PUBLISHED_AT = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)


class MemoryPipeline:
    def __init__(self, redis: MemoryRedis) -> None:
        self._redis = redis
        self._pending: list[tuple[str, dict[str, str]]] = []

    def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> MemoryPipeline:
        assert key == agent_run_live_stream_key(RUN_ID)
        assert maxlen == AGENT_RUN_LIVE_STREAM_MAXLEN
        assert approximate is False
        self._pending.append((key, fields))
        return self

    def expire(self, key: str, seconds: int) -> MemoryPipeline:
        assert key == agent_run_live_stream_key(RUN_ID)
        assert seconds == AGENT_RUN_LIVE_STREAM_TTL_SECONDS
        return self

    async def execute(self) -> list[object]:
        ids: list[str] = []
        for _key, fields in self._pending:
            stream_id = f"{len(self._redis.entries) + 1}-0"
            self._redis.entries.append((stream_id, fields))
            ids.append(stream_id)
        if self._redis.hang_after_append_once:
            self._redis.hang_after_append_once = False
            await _sleep_forever()
        return [*ids, True]


class MemoryRedis:
    def __init__(
        self,
        entries: list[tuple[str, dict[str, str]]] | None = None,
        *,
        exists: int | None = None,
    ) -> None:
        self.entries = entries or []
        self._exists = exists
        self.hang_after_append_once = False

    def pipeline(self) -> MemoryPipeline:
        return MemoryPipeline(self)

    async def xrange(
        self,
        _key: str,
        _min: str = "-",
        _max: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        entries = self.entries
        if count is not None:
            entries = entries[:count]
        return entries

    async def xread(
        self,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        assert count == AGENT_RUN_LIVE_STREAM_PAGE_SIZE
        assert block is None
        cursor = next(iter(streams.values()))
        return [
            (
                agent_run_live_stream_key(RUN_ID),
                [
                    entry
                    for entry in self.entries
                    if is_stream_id_before(cursor, entry[0])
                ],
            )
        ]

    async def exists(self, _key: str) -> int:
        return len(self.entries) if self._exists is None else self._exists


class RaisingRedis(MemoryRedis):
    def pipeline(self) -> MemoryPipeline:
        raise RedisConnectionError("redis down SECRET_ANSWER")

    async def xrange(self, *_args: object, **_kwargs: object) -> list[object]:
        raise RedisConnectionError("redis down SECRET_ANSWER")


class DelayedRedis(MemoryRedis):
    async def xrange(self, *_args: object, **_kwargs: object) -> list[object]:
        await asyncio.sleep(0.03)
        return []

    async def exists(self, _key: str) -> int:
        await asyncio.sleep(0.03)
        return 0


class UnexpectedRedisAccess:
    def __getattr__(self, _name: str) -> object:
        raise AssertionError("Redis must not be accessed")


async def _sleep_forever() -> None:
    while True:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_stream_round_trip_filters_epoch_and_skips_bad_payload() -> None:
    redis = MemoryRedis()
    publisher_1 = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_1)
    publisher_2 = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_2)

    assert await publisher_1.begin_attempt() == "1-0"
    assert (
        await publisher_1.publish(AgentRunLiveStreamStageEvent(stage="planning"))
        == "2-0"
    )
    assert await publisher_2.begin_attempt() == "3-0"
    assert (
        await publisher_2.publish(
            AgentRunLiveStreamActivityEvent(
                event=InternalSearchStartedEvent(query_count=2)
            )
        )
        == "4-0"
    )
    assert (
        await publisher_2.publish(
            AgentRunLiveStreamAnswerDeltaEvent(generation=1, text="draft")
        )
        == "5-0"
    )
    assert (
        await publisher_2.publish(AgentRunLiveStreamAnswerResetEvent(generation=2))
        == "6-0"
    )
    assert (
        await publisher_2.publish(AgentRunLiveStreamTerminalEvent(status="completed"))
        == "7-0"
    )

    # The envelope can be valid while one event payload is invalid. It must not
    # prevent the other current-epoch entries from being returned.
    redis.entries.extend(
        [
            (
                "8-0",
                {
                    "type": "future.event",
                    "attemptEpoch": str(EPOCH_2),
                    "publishedAt": PUBLISHED_AT.isoformat(),
                    "payload": "{}",
                },
            ),
            (
                "9-0",
                {
                    "type": "stage",
                    "attemptEpoch": str(EPOCH_2),
                    "publishedAt": PUBLISHED_AT.isoformat(),
                    "payload": "not-json",
                },
            ),
            (
                "10-0",
                {
                    "type": "attempt.started",
                    "attemptEpoch": str(EPOCH_2),
                    "publishedAt": PUBLISHED_AT.isoformat(),
                    "payload": '{"unexpected":true}',
                },
            ),
        ]
    )

    result = await AgentRunLiveStreamReader(redis).read_after(RUN_ID, EPOCH_2, None)

    assert result.status is AgentRunLiveStreamReadStatus.EVENTS
    assert [entry.stream_id for entry in result.events] == [
        "3-0",
        "4-0",
        "5-0",
        "6-0",
        "7-0",
    ]
    assert [entry.event.type for entry in result.events] == [
        "attempt.started",
        "activity",
        "answer.delta",
        "answer.reset",
        "terminal",
    ]
    assert all(entry.attempt_epoch == EPOCH_2 for entry in result.events)
    assert all("attemptEpoch" in fields for _id, fields in redis.entries)


@pytest.mark.asyncio
async def test_reader_distinguishes_degradation_results() -> None:
    reader = AgentRunLiveStreamReader(MemoryRedis(exists=0))
    missing = await reader.read_after(RUN_ID, EPOCH_1, None)
    assert missing.status is AgentRunLiveStreamReadStatus.STREAM_MISSING

    absent = await AgentRunLiveStreamReader(MemoryRedis(exists=1)).read_after(
        RUN_ID, EPOCH_1, None
    )
    assert absent.status is AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT

    trimmed = await AgentRunLiveStreamReader(
        MemoryRedis(entries=[("10-0", _envelope(EPOCH_1))])
    ).read_after(RUN_ID, EPOCH_1, "9-0")
    assert trimmed.status is AgentRunLiveStreamReadStatus.CURSOR_TRIMMED

    empty = await AgentRunLiveStreamReader(
        MemoryRedis(entries=[("10-0", _envelope(EPOCH_1))])
    ).read_after(RUN_ID, EPOCH_1, "10-0")
    assert empty.status is AgentRunLiveStreamReadStatus.EMPTY

    unavailable = await AgentRunLiveStreamReader(RaisingRedis()).read_after(
        RUN_ID, EPOCH_1, None
    )
    assert unavailable.status is AgentRunLiveStreamReadStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_reader_skips_only_older_epochs_and_advances_cursor() -> None:
    redis = MemoryRedis(
        entries=[
            ("1-0", _envelope(EPOCH_1)),
            ("2-0", _envelope(EPOCH_1)),
        ]
    )
    reader = AgentRunLiveStreamReader(redis)

    absent = await reader.read_after(RUN_ID, EPOCH_2, None)
    after_skipped = await reader.read_after(
        RUN_ID,
        EPOCH_2,
        absent.next_cursor,
    )

    assert absent.status is AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
    assert absent.next_cursor == "2-0"
    assert after_skipped.status is AgentRunLiveStreamReadStatus.EMPTY


@pytest.mark.asyncio
async def test_follow_read_continues_after_only_zombie_entries() -> None:
    redis = MemoryRedis(
        entries=[
            ("1-0", _envelope(EPOCH_2)),
            ("2-0", _envelope(EPOCH_1)),
        ]
    )
    reader = AgentRunLiveStreamReader(redis)

    zombies = await reader.read_after(RUN_ID, EPOCH_2, "1-0")
    redis.entries.append(("3-0", _envelope(EPOCH_2, event_type="stage")))
    resumed = await reader.read_after(RUN_ID, EPOCH_2, zombies.next_cursor)

    assert zombies.status is AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
    assert zombies.next_cursor == "2-0"
    assert resumed.status is AgentRunLiveStreamReadStatus.EVENTS
    assert [entry.stream_id for entry in resumed.events] == ["3-0"]


@pytest.mark.asyncio
async def test_reader_reports_newer_epoch_without_consuming_boundary() -> None:
    redis = MemoryRedis(
        entries=[
            ("1-0", _envelope(EPOCH_1)),
            ("2-0", _envelope(EPOCH_3)),
        ]
    )

    result = await AgentRunLiveStreamReader(redis).read_after(
        RUN_ID,
        EPOCH_1,
        "1-0",
    )

    assert result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED
    assert result.events == ()
    assert result.observed_attempt_epoch == EPOCH_3
    assert result.next_cursor == "1-0"


@pytest.mark.asyncio
async def test_reader_returns_current_before_boundary_and_replays_new_attempt() -> None:
    redis = MemoryRedis(
        entries=[
            ("1-0", _envelope(EPOCH_1)),
            ("2-0", _envelope(EPOCH_1, event_type="stage")),
            ("3-0", _envelope(EPOCH_3)),
            ("4-0", _envelope(EPOCH_1, event_type="stage")),
            ("5-0", _envelope(EPOCH_3, event_type="stage")),
        ]
    )
    reader = AgentRunLiveStreamReader(redis)

    current = await reader.read_after(RUN_ID, EPOCH_1, None)
    advanced = await reader.read_after(RUN_ID, EPOCH_1, current.next_cursor)
    new_attempt = await reader.read_after(RUN_ID, EPOCH_3, None)

    assert current.status is AgentRunLiveStreamReadStatus.EVENTS
    assert [entry.stream_id for entry in current.events] == ["1-0", "2-0"]
    assert current.next_cursor == "2-0"
    assert advanced.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED
    assert advanced.next_cursor == "2-0"
    assert advanced.observed_attempt_epoch == EPOCH_3
    assert [entry.stream_id for entry in new_attempt.events] == ["3-0", "5-0"]
    assert {entry.attempt_epoch for entry in new_attempt.events} == {EPOCH_3}


@pytest.mark.asyncio
async def test_reader_uses_valid_envelope_epoch_when_marker_payload_is_broken() -> None:
    redis = MemoryRedis(
        entries=[
            (
                "1-0",
                _envelope(
                    EPOCH_3,
                    payload='{ "unexpected": true }',
                ),
            )
        ]
    )

    result = await AgentRunLiveStreamReader(redis).read_after(
        RUN_ID,
        EPOCH_1,
        None,
    )

    assert result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED
    assert result.observed_attempt_epoch == EPOCH_3
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_reader_ignores_malformed_diagnostic_timestamp() -> None:
    current = _envelope(EPOCH_1, event_type="stage")
    current["publishedAt"] = "not-a-timestamp"
    advanced = _envelope(EPOCH_3)
    advanced["publishedAt"] = "also-not-a-timestamp"
    redis = MemoryRedis(entries=[("1-0", current), ("2-0", advanced)])
    reader = AgentRunLiveStreamReader(redis)

    current_result = await reader.read_after(RUN_ID, EPOCH_1, None)
    advanced_result = await reader.read_after(
        RUN_ID,
        EPOCH_1,
        current_result.next_cursor,
    )

    assert [entry.stream_id for entry in current_result.events] == ["1-0"]
    assert advanced_result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED
    assert advanced_result.observed_attempt_epoch == EPOCH_3


@pytest.mark.asyncio
async def test_reader_does_not_decode_payload_for_other_epochs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = MemoryRedis(
        entries=[
            ("1-0", _envelope(EPOCH_1, payload="not-json")),
            ("2-0", _envelope(EPOCH_3, payload="not-json")),
        ]
    )

    def fail_if_called(_payload: str) -> object:
        raise AssertionError("payload decode must follow epoch classification")

    monkeypatch.setattr("app.agent.live_updates.stream.json.loads", fail_if_called)
    result = await AgentRunLiveStreamReader(redis).read_after(
        RUN_ID,
        EPOCH_2,
        None,
    )

    assert result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED
    assert result.next_cursor == "1-0"


def test_read_result_enforces_attempt_advanced_invariant() -> None:
    with pytest.raises(ValueError, match="observed attempt epoch"):
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
        )
    with pytest.raises(ValueError, match="only valid for attempt advanced"):
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            observed_attempt_epoch=EPOCH_2,
        )


@pytest.mark.asyncio
async def test_reader_rejects_nonpositive_requested_epoch_before_redis_access() -> None:
    reader = AgentRunLiveStreamReader(UnexpectedRedisAccess())

    with pytest.raises(ValueError, match="attempt epoch must be positive"):
        await reader.read_after(RUN_ID, 0, None)
    with pytest.raises(ValueError, match="attempt epoch must be positive"):
        await reader.read_after(RUN_ID, -1, None)


@pytest.mark.asyncio
async def test_old_timestamp_only_stream_is_attempt_absent() -> None:
    redis = MemoryRedis(
        entries=[
            (
                "1-0",
                {
                    "type": "attempt.started",
                    "attemptEpoch": PUBLISHED_AT.isoformat(),
                    "publishedAt": PUBLISHED_AT.isoformat(),
                    "payload": "{}",
                },
            )
        ]
    )

    result = await AgentRunLiveStreamReader(redis).read_after(
        RUN_ID,
        EPOCH_1,
        None,
    )

    assert result.status is AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT
    assert result.next_cursor == "1-0"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_epoch", ["0", "-1", "1.5", "true", "abc"])
async def test_reader_skips_invalid_integer_epoch_strings(
    invalid_epoch: str,
) -> None:
    invalid = _envelope(EPOCH_1)
    invalid["attemptEpoch"] = invalid_epoch
    redis = MemoryRedis(
        entries=[
            ("1-0", invalid),
            ("2-0", _envelope(EPOCH_1)),
        ]
    )

    result = await AgentRunLiveStreamReader(redis).read_after(
        RUN_ID,
        EPOCH_1,
        None,
    )

    assert [entry.stream_id for entry in result.events] == ["2-0"]


@pytest.mark.parametrize("attempt_epoch", [0, -1])
def test_publisher_rejects_nonpositive_attempt_epoch(attempt_epoch: int) -> None:
    with pytest.raises(ValueError, match="attempt epoch must be positive"):
        AgentRunLiveStreamPublisher(UnexpectedRedisAccess(), RUN_ID, attempt_epoch)


@pytest.mark.asyncio
async def test_lazy_retry_allows_same_epoch_marker_duplicate() -> None:
    redis = MemoryRedis()
    redis.hang_after_append_once = True
    publisher = AgentRunLiveStreamPublisher(
        redis,
        RUN_ID,
        EPOCH_1,
        timeout_seconds=0.01,
    )

    start = time.monotonic()
    assert await publisher.begin_attempt() is None
    assert time.monotonic() - start < 0.05
    assert (
        await publisher.publish(AgentRunLiveStreamStageEvent(stage="planning")) == "3-0"
    )

    result = await AgentRunLiveStreamReader(redis).read_after(RUN_ID, EPOCH_1, None)

    assert result.status is AgentRunLiveStreamReadStatus.EVENTS
    assert [entry.event.type for entry in result.events] == [
        "attempt.started",
        "attempt.started",
        "stage",
    ]
    assert {entry.attempt_epoch for entry in result.events} == {EPOCH_1}


@pytest.mark.asyncio
async def test_logical_read_timeout_is_not_per_redis_command() -> None:
    reader = AgentRunLiveStreamReader(DelayedRedis(), timeout_seconds=0.04)

    start = time.monotonic()
    result = await reader.read_after(RUN_ID, EPOCH_1, None)
    elapsed = time.monotonic() - start

    assert result.status is AgentRunLiveStreamReadStatus.UNAVAILABLE
    assert elapsed < 0.07


@pytest.mark.asyncio
async def test_failures_do_not_leak_payload_or_redis_error_to_logs() -> None:
    publisher = AgentRunLiveStreamPublisher(RaisingRedis(), RUN_ID, EPOCH_1)
    reader = AgentRunLiveStreamReader(RaisingRedis())

    with capture_logs() as logs:
        assert (
            await publisher.publish(
                AgentRunLiveStreamAnswerDeltaEvent(
                    generation=1,
                    text="SECRET_ANSWER",
                )
            )
            is None
        )
        result = await reader.read_after(RUN_ID, EPOCH_1, None)

    assert result.status is AgentRunLiveStreamReadStatus.UNAVAILABLE
    assert [entry["event"] for entry in logs] == [
        "agent_run_live_stream_publish_failed",
        "agent_run_live_stream_read_failed",
    ]
    assert "SECRET_ANSWER" not in repr(logs)
    assert "redis down" not in repr(logs)


def test_stream_id_comparison_is_numeric_pair_ordering() -> None:
    assert is_stream_id_before("1-9", "1-10")
    assert is_stream_id_before("9-0", "10-0")
    assert not is_stream_id_before("1-10", "1-9")
    assert not is_stream_id_before("10-0", "9-0")
    assert AGENT_RUN_LIVE_STREAM_MAXLEN == 4096
    assert AGENT_RUN_LIVE_STREAM_PAGE_SIZE == 128
    assert AGENT_RUN_LIVE_STREAM_TTL_SECONDS == 900
    assert AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS == 0.5
    assert agent_run_live_stream_key(RUN_ID) == f"agent:run:{RUN_ID}:live"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_orders_entries_and_readers_replay_independently() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_1)
        reader = AgentRunLiveStreamReader(redis)

        marker_id = await publisher.begin_attempt()
        stage_id = await publisher.publish(
            AgentRunLiveStreamStageEvent(stage="planning")
        )
        first = await reader.read_after(RUN_ID, EPOCH_1, None)
        second = await reader.read_after(RUN_ID, EPOCH_1, None)

        assert marker_id is not None
        assert stage_id is not None
        assert is_stream_id_before(marker_id, stage_id)
        assert first.status is AgentRunLiveStreamReadStatus.EVENTS
        assert second.status is AgentRunLiveStreamReadStatus.EVENTS
        assert first.events == second.events
        assert [entry.stream_id for entry in first.events] == [marker_id, stage_id]
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_excludes_zombie_epoch_and_allows_duplicate_marker() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        old_attempt = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_1)
        current_attempt = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_2)

        await old_attempt.begin_attempt()
        await old_attempt.publish(AgentRunLiveStreamStageEvent(stage="planning"))
        await current_attempt.begin_attempt()
        await old_attempt.publish(AgentRunLiveStreamStageEvent(stage="retrieving"))
        await AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_2).begin_attempt()

        result = await AgentRunLiveStreamReader(redis).read_after(RUN_ID, EPOCH_2, None)

        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        assert [entry.event.type for entry in result.events] == [
            "attempt.started",
            "attempt.started",
        ]
        assert {entry.attempt_epoch for entry in result.events} == {EPOCH_2}
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_marker_trim_keeps_epoch_filter_and_flags_old_cursor() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_2)
        marker_id = await publisher.begin_attempt()
        await publisher.publish(AgentRunLiveStreamStageEvent(stage="synthesizing"))
        assert marker_id is not None
        await redis.xtrim(
            agent_run_live_stream_key(RUN_ID),
            maxlen=1,
            approximate=False,
        )

        current = await AgentRunLiveStreamReader(redis).read_after(
            RUN_ID, EPOCH_2, None
        )
        trimmed = await AgentRunLiveStreamReader(redis).read_after(
            RUN_ID, EPOCH_2, marker_id
        )

        assert current.status is AgentRunLiveStreamReadStatus.EVENTS
        assert [entry.event.type for entry in current.events] == ["stage"]
        assert trimmed.status is AgentRunLiveStreamReadStatus.CURSOR_TRIMMED
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_pages_current_epoch_and_enforces_cap_and_ttl() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_1)
        await publisher.begin_attempt()
        for _ in range(130):
            await publisher.publish(AgentRunLiveStreamStageEvent(stage="planning"))

        reader = AgentRunLiveStreamReader(redis)
        first_page = await reader.read_after(RUN_ID, EPOCH_1, None)
        second_page = await reader.read_after(
            RUN_ID,
            EPOCH_1,
            first_page.next_cursor,
        )

        assert first_page.status is AgentRunLiveStreamReadStatus.EVENTS
        assert len(first_page.events) == AGENT_RUN_LIVE_STREAM_PAGE_SIZE
        assert second_page.status is AgentRunLiveStreamReadStatus.EVENTS
        assert len(second_page.events) == 3
        assert await redis.ttl(agent_run_live_stream_key(RUN_ID)) in range(
            1,
            AGENT_RUN_LIVE_STREAM_TTL_SECONDS + 1,
        )

        pipeline = redis.pipeline()
        for _ in range(AGENT_RUN_LIVE_STREAM_MAXLEN + 1):
            pipeline.xadd(
                agent_run_live_stream_key(RUN_ID),
                _envelope(EPOCH_1),
                maxlen=AGENT_RUN_LIVE_STREAM_MAXLEN,
                approximate=False,
            )
        pipeline.expire(
            agent_run_live_stream_key(RUN_ID), AGENT_RUN_LIVE_STREAM_TTL_SECONDS
        )
        await pipeline.execute()

        assert await redis.xlen(agent_run_live_stream_key(RUN_ID)) == (
            AGENT_RUN_LIVE_STREAM_MAXLEN
        )
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_passes_events_after_terminal() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.flushdb()
        publisher = AgentRunLiveStreamPublisher(redis, RUN_ID, EPOCH_1)
        await publisher.begin_attempt()
        await publisher.publish(AgentRunLiveStreamTerminalEvent(status="completed"))
        await publisher.publish(AgentRunLiveStreamStageEvent(stage="retrieving"))

        result = await AgentRunLiveStreamReader(redis).read_after(RUN_ID, EPOCH_1, None)

        assert result.status is AgentRunLiveStreamReadStatus.EVENTS
        assert [entry.event.type for entry in result.events] == [
            "attempt.started",
            "terminal",
            "stage",
        ]
    finally:
        await redis.flushdb()
        await redis.aclose()


def _envelope(
    epoch: int,
    *,
    event_type: str = "attempt.started",
    payload: str | None = None,
) -> dict[str, str]:
    if payload is None:
        payload = '{"stage":"planning"}' if event_type == "stage" else "{}"
    return {
        "type": event_type,
        "attemptEpoch": str(epoch),
        "publishedAt": PUBLISHED_AT.isoformat(),
        "payload": payload,
    }
