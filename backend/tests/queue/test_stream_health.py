"""pipeline Stream health snapshot のread-only契約テスト。"""

from __future__ import annotations

import importlib
from dataclasses import fields
from types import ModuleType
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError


def _health_module() -> ModuleType:
    """未実装moduleをcollection errorではなく契約failureとして報告する。"""
    try:
        return importlib.import_module("app.queue.stream_health")
    except ModuleNotFoundError as exc:
        if exc.name == "app.queue.stream_health":
            pytest.fail("app.queue.stream_health is not implemented")
        raise


def _target(module: ModuleType, stage: str = "curation") -> Any:
    stream = f"pipeline:{stage}"
    return module.StreamHealthTarget(stage=stage, stream=stream, group="taskiq")


def _group(
    *,
    name: str = "taskiq",
    lag: int | None = 0,
    pending: int = 0,
    last_delivered_id: str = "0-0",
) -> dict[str, object]:
    return {
        "name": name,
        "lag": lag,
        "pending": pending,
        "last-delivered-id": last_delivered_id,
    }


def _pending(
    message_id: str,
    *,
    idle_ms: int = 0,
    consumer: str = "consumer-uuid-must-not-leak",
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "consumer": consumer,
        "time_since_delivered": idle_ms,
        "times_delivered": 1,
    }


class _RecordingPipeline:
    def __init__(self, redis: _RecordingRedis, result: object) -> None:
        self._redis = redis
        self._result = result

    async def __aenter__(self) -> _RecordingPipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def time(self) -> _RecordingPipeline:
        self._redis.calls.append(("TIME",))
        return self

    def xlen(self, name: str) -> _RecordingPipeline:
        self._redis.calls.append(("XLEN", name))
        return self

    def xinfo_groups(self, name: str) -> _RecordingPipeline:
        self._redis.calls.append(("XINFO GROUPS", name))
        return self

    def xpending_range(
        self,
        name: str,
        groupname: str,
        min: str,
        max: str,
        count: int,
        consumername: str | None = None,
        idle: int | None = None,
    ) -> _RecordingPipeline:
        self._redis.calls.append(
            (
                "XPENDING",
                name,
                groupname,
                min,
                max,
                count,
                consumername,
                idle,
            )
        )
        return self

    async def execute(self) -> list[object]:
        self._redis.calls.append(("EXEC",))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result  # type: ignore[return-value]


class _RecordingRedis:
    def __init__(
        self,
        *,
        pipeline_results: list[object],
        xrange_results: list[object] | None = None,
        idle_result: object = None,
    ) -> None:
        self.pipeline_results = list(pipeline_results)
        self.xrange_results = list(xrange_results or [])
        self.idle_result = [] if idle_result is None else idle_result
        self.calls: list[tuple[object, ...]] = []

    def pipeline(self, transaction: bool = True) -> _RecordingPipeline:
        self.calls.append(("pipeline", transaction))
        if not self.pipeline_results:
            raise AssertionError("unexpected pipeline read")
        return _RecordingPipeline(self, self.pipeline_results.pop(0))

    async def xrange(
        self,
        name: str,
        min: str = "-",
        max: str = "+",
        count: int | None = None,
    ) -> object:
        self.calls.append(("XRANGE", name, min, max, count))
        if not self.xrange_results:
            raise AssertionError("unexpected XRANGE")
        result = self.xrange_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def xpending_range(
        self,
        name: str,
        groupname: str,
        min: str,
        max: str,
        count: int,
        consumername: str | None = None,
        idle: int | None = None,
    ) -> object:
        self.calls.append(
            (
                "XPENDING",
                name,
                groupname,
                min,
                max,
                count,
                consumername,
                idle,
            )
        )
        if isinstance(self.idle_result, BaseException):
            raise self.idle_result
        return self.idle_result


def test_pipeline_queue_targets_are_the_fixed_two_stage_contracts() -> None:
    module = _health_module()

    assert tuple(
        (target.stage, target.stream, target.group)
        for target in module.PIPELINE_QUEUE_TARGETS
    ) == (
        ("curation", "pipeline:curation", "taskiq"),
        ("assessment", "pipeline:assessment", "taskiq"),
    )


@pytest.mark.asyncio
async def test_empty_stream_snapshot_has_zero_counts_and_no_ages() -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[[(1_000, 250_000), 0, [_group()], []]],
    )

    snapshot = await module.read_stream_health(redis, target)
    public_fields = tuple(field.name for field in fields(snapshot))

    assert (
        public_fields,
        tuple(getattr(snapshot, name) for name in public_fields),
    ) == (
        (
            "stage",
            "stream",
            "group",
            "observation_timestamp",
            "retained_entries",
            "lag",
            "pending",
            "oldest_undelivered_enqueue_age",
            "oldest_pending_enqueue_age",
            "oldest_outstanding_enqueue_age",
        ),
        (
            "curation",
            "pipeline:curation",
            "taskiq",
            1_000.25,
            0,
            0,
            0,
            None,
            None,
            None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("zero", "one"),
    [
        (0, 1),
        ("0", "1"),
        (b"0", b"1"),
    ],
)
async def test_redis_integer_encodings_are_normalized(
    zero: int | str | bytes,
    one: int | str | bytes,
) -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[
            [
                (one, zero),
                one,
                [
                    {
                        "name": "taskiq",
                        "lag": one,
                        "pending": one,
                        "last-delivered-id": "0-0",
                    }
                ],
                [_pending("1-0")],
            ]
        ],
        xrange_results=[[("1-0", {})]],
    )

    snapshot = await module.read_stream_health(redis, target)

    assert (
        snapshot.observation_timestamp,
        snapshot.retained_entries,
        snapshot.lag,
        snapshot.pending,
    ) == (1.0, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pipeline_result",
    [
        [(1_000, 0), object(), [_group()], []],
        [(1_000, 0), 1, [_group(lag=True)], []],
        [
            (1_000, 0),
            1,
            [
                {
                    "name": "taskiq",
                    "lag": 0,
                    "pending": "not-a-number",
                    "last-delivered-id": "0-0",
                }
            ],
            [],
        ],
    ],
)
async def test_invalid_redis_integer_is_inconsistent_snapshot(
    pipeline_result: list[object],
) -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(pipeline_results=[pipeline_result])

    with pytest.raises(module.StreamHealthError) as raised:
        await module.read_stream_health(redis, target)

    assert (raised.value.stage, raised.value.reason) == (
        "curation",
        "inconsistent_snapshot",
    )


@pytest.mark.asyncio
async def test_snapshot_uses_one_transaction_exact_group_and_enqueue_ages() -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[
            [
                (1_000, 500_000),
                10,
                [
                    _group(
                        name="another-group",
                        lag=999,
                        pending=999,
                        last_delivered_id="1-0",
                    ),
                    _group(
                        lag=2,
                        pending=1,
                        last_delivered_id="990000-0",
                    ),
                ],
                [_pending("980000-0", idle_ms=987_654)],
            ]
        ],
        xrange_results=[[("995000-0", {"data": "must-not-leak"})]],
    )

    snapshot = await module.read_stream_health(redis, target)

    assert (
        snapshot.retained_entries,
        snapshot.lag,
        snapshot.pending,
        snapshot.oldest_undelivered_enqueue_age,
        snapshot.oldest_pending_enqueue_age,
        snapshot.oldest_outstanding_enqueue_age,
        redis.calls,
    ) == (
        10,
        2,
        1,
        5.5,
        20.5,
        20.5,
        [
            ("pipeline", True),
            ("TIME",),
            ("XLEN", "pipeline:curation"),
            ("XINFO GROUPS", "pipeline:curation"),
            (
                "XPENDING",
                "pipeline:curation",
                "taskiq",
                "-",
                "+",
                1,
                None,
                None,
            ),
            ("EXEC",),
            ("XRANGE", "pipeline:curation", "(990000-0", "+", 1),
        ],
    )


@pytest.mark.asyncio
async def test_pending_age_uses_min_id_not_its_delivery_idle() -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[
            [
                (1_000, 0),
                1,
                [_group(lag=0, pending=1, last_delivered_id="900000-0")],
                [_pending("990000-0", idle_ms=900_000)],
            ]
        ]
    )

    snapshot = await module.read_stream_health(redis, target)

    assert (
        snapshot.oldest_pending_enqueue_age,
        snapshot.oldest_outstanding_enqueue_age,
        "idle" in {field.name for field in fields(snapshot)},
        any(call[-1] is not None for call in redis.calls if call[0] == "XPENDING"),
    ) == (10.0, 10.0, False, False)


@pytest.mark.asyncio
async def test_future_stream_ids_clamp_enqueue_ages_to_zero() -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[
            [
                (1_000, 0),
                2,
                [_group(lag=1, pending=1, last_delivered_id="1000000-0")],
                [_pending("1001000-0")],
            ]
        ],
        xrange_results=[[("1002000-0", {})]],
    )

    snapshot = await module.read_stream_health(redis, target)

    assert (
        snapshot.oldest_undelivered_enqueue_age,
        snapshot.oldest_pending_enqueue_age,
        snapshot.oldest_outstanding_enqueue_age,
    ) == (0.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_inconsistent_xrange_is_reread_once_then_succeeds() -> None:
    module = _health_module()
    target = _target(module)
    transaction = [
        (1_000, 0),
        1,
        [_group(lag=1, pending=0, last_delivered_id="900000-0")],
        [],
    ]
    redis = _RecordingRedis(
        pipeline_results=[transaction, transaction],
        xrange_results=[[], [("950000-0", {})]],
    )

    snapshot = await module.read_stream_health(redis, target)

    assert (
        snapshot.oldest_undelivered_enqueue_age,
        sum(call == ("pipeline", True) for call in redis.calls),
        sum(call[0] == "XRANGE" for call in redis.calls),
    ) == (50.0, 2, 2)


@pytest.mark.asyncio
async def test_inconsistent_xrange_after_one_reread_is_failure() -> None:
    module = _health_module()
    target = _target(module)
    transaction = [
        (1_000, 0),
        1,
        [_group(lag=1, pending=0, last_delivered_id="900000-0")],
        [],
    ]
    redis = _RecordingRedis(
        pipeline_results=[transaction, transaction],
        xrange_results=[[], []],
    )

    with pytest.raises(module.StreamHealthError) as raised:
        await module.read_stream_health(redis, target)

    assert (
        raised.value.stage,
        raised.value.reason,
        sum(call == ("pipeline", True) for call in redis.calls),
        sum(call[0] == "XRANGE" for call in redis.calls),
    ) == ("curation", "inconsistent_snapshot", 2, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipeline_result", "reason"),
    [
        (ResponseError("no such key"), "stream_missing"),
        (
            [(1_000, 0), 1, [_group(name="other")], []],
            "group_missing",
        ),
        (
            [(1_000, 0), 1, [_group(lag=None)], []],
            "lag_unknown",
        ),
        (ResponseError("WRONGTYPE operation against a key"), "redis_unavailable"),
        (RedisConnectionError("connection refused"), "redis_unavailable"),
    ],
)
async def test_snapshot_failures_use_fixed_nonzero_reasons(
    pipeline_result: object,
    reason: str,
) -> None:
    module = _health_module()
    target = _target(module, "assessment")
    redis = _RecordingRedis(pipeline_results=[pipeline_result])

    with pytest.raises(module.StreamHealthError) as raised:
        await module.read_stream_health(redis, target)

    public_state = vars(raised.value)
    assert (
        raised.value.stage,
        raised.value.reason,
        {"payload", "task_id", "consumer", "consumer_uuid"} & public_state.keys(),
    ) == ("assessment", reason, set())


@pytest.mark.asyncio
async def test_post_transaction_redis_failure_is_redis_unavailable() -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(
        pipeline_results=[
            [
                (1_000, 0),
                1,
                [_group(lag=1, pending=0, last_delivered_id="900000-0")],
                [],
            ]
        ],
        xrange_results=[RedisConnectionError("connection refused")],
    )

    with pytest.raises(module.StreamHealthError) as raised:
        await module.read_stream_health(redis, target)

    assert (raised.value.stage, raised.value.reason) == (
        "curation",
        "redis_unavailable",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_result", [[], [_pending("900000-0", idle_ms=700_000)]])
async def test_idle_diagnostic_is_explicit_bounded_existence_check(
    idle_result: list[dict[str, object]],
) -> None:
    module = _health_module()
    target = _target(module)
    redis = _RecordingRedis(pipeline_results=[], idle_result=idle_result)

    exists = await module.has_idle_pending(redis, target, idle_ms=600_000)

    assert (exists, redis.calls) == (
        bool(idle_result),
        [
            (
                "XPENDING",
                "pipeline:curation",
                "taskiq",
                "-",
                "+",
                1,
                None,
                600_000,
            )
        ],
    )
