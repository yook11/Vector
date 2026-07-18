"""acquisition / completion / curation / assessment の4 stageを読む
Redis Stream health snapshot。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NoReturn

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

StreamHealthStage = Literal[
    "acquisition",
    "completion",
    "curation",
    "assessment",
]
StreamHealthFailureReason = Literal[
    "stream_missing",
    "group_missing",
    "lag_unknown",
    "redis_unavailable",
    "inconsistent_snapshot",
]


@dataclass(frozen=True, slots=True)
class StreamHealthTarget:
    """観測対象のstage、Stream、consumer groupを保持する。"""

    stage: StreamHealthStage
    stream: str
    group: str


@dataclass(frozen=True, slots=True)
class StreamHealthSnapshot:
    """同じRedis時刻を基準にしたstage別Stream snapshot。"""

    stage: StreamHealthStage
    stream: str
    group: str
    observation_timestamp: float
    retained_entries: int
    lag: int
    pending: int
    oldest_undelivered_enqueue_age: float | None
    oldest_pending_enqueue_age: float | None
    oldest_outstanding_enqueue_age: float | None


class StreamHealthError(RuntimeError):
    """0件と区別して扱う必要があるStream観測失敗。"""

    def __init__(
        self,
        stage: str,
        reason: StreamHealthFailureReason,
    ) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f"{stage}: {reason}")


PIPELINE_QUEUE_TARGETS = (
    StreamHealthTarget(
        stage="acquisition",
        stream="pipeline:acquisition",
        group="taskiq",
    ),
    StreamHealthTarget(
        stage="completion",
        stream="pipeline:completion",
        group="taskiq",
    ),
    StreamHealthTarget(
        stage="curation",
        stream="pipeline:curation",
        group="taskiq",
    ),
    StreamHealthTarget(
        stage="assessment",
        stream="pipeline:assessment",
        group="taskiq",
    ),
)


class _UndeliveredEntryMissing(Exception):
    """lagとpost-transaction XRANGEが一時的に矛盾したことを表す。"""


def _mapping_value(mapping: Mapping[object, object], key: str) -> object:
    """decode_responses設定に依存せずRedis response fieldを読む。"""
    if key in mapping:
        return mapping[key]
    encoded_key = key.encode()
    if encoded_key in mapping:
        return mapping[encoded_key]
    raise KeyError(key)


def _decoded(value: object) -> str:
    """Redis responseのbytesまたは文字列を文字列へ正規化する。"""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _parse_redis_int(value: object) -> int:
    """Redis responseで許可する整数表現だけをintへ変換する。"""
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes)):
        raise TypeError(f"invalid Redis integer type: {type(value).__name__}")
    return int(value)


def _timestamp(redis_time: object) -> float:
    """Redis TIME responseをUnix timestamp秒へ変換する。"""
    if not isinstance(redis_time, Sequence) or len(redis_time) != 2:
        raise ValueError("invalid Redis TIME response")
    seconds, microseconds = redis_time
    return _parse_redis_int(seconds) + _parse_redis_int(microseconds) / 1_000_000


def _enqueue_age(observed_at: float, message_id: object) -> float:
    """Stream IDのmillisecondsから負にならないenqueue age秒を算出する。"""
    milliseconds = int(_decoded(message_id).split("-", maxsplit=1)[0])
    return max(0.0, observed_at - milliseconds / 1_000)


def _failure_reason(exc: ResponseError) -> StreamHealthFailureReason:
    """Redis command errorを固定された観測失敗理由へ写像する。"""
    message = str(exc).lower()
    if "nogroup" in message:
        return "group_missing"
    if "no such key" in message:
        return "stream_missing"
    return "redis_unavailable"


def _raise_stream_health_error(
    target: StreamHealthTarget,
    exc: RedisError,
) -> NoReturn:
    """Redis例外をpayloadを持たない観測失敗へ変換する。"""
    reason: StreamHealthFailureReason = "redis_unavailable"
    if isinstance(exc, ResponseError):
        reason = _failure_reason(exc)
    raise StreamHealthError(stage=target.stage, reason=reason) from exc


async def _read_stream_health_once(
    redis: Redis,
    target: StreamHealthTarget,
) -> StreamHealthSnapshot:
    """1回のtransactionと必要時のXRANGEでsnapshotを読む。"""
    try:
        async with redis.pipeline(transaction=True) as pipeline:
            pipeline.time()
            pipeline.xlen(target.stream)
            pipeline.xinfo_groups(target.stream)
            pipeline.xpending_range(
                target.stream,
                target.group,
                min="-",
                max="+",
                count=1,
            )
            transaction = await pipeline.execute()
    except RedisError as exc:
        _raise_stream_health_error(target, exc)

    try:
        redis_time, retained_result, groups_result, pending_result = transaction
        observed_at = _timestamp(redis_time)
        retained = _parse_redis_int(retained_result)
        groups = list(groups_result)
        pending_entries = list(pending_result)
    except (TypeError, ValueError) as exc:
        raise StreamHealthError(
            stage=target.stage,
            reason="inconsistent_snapshot",
        ) from exc

    group: Mapping[object, object] | None = None
    for candidate in groups:
        if not isinstance(candidate, Mapping):
            continue
        try:
            name = _decoded(_mapping_value(candidate, "name"))
        except KeyError:
            continue
        if name == target.group:
            group = candidate
            break

    if group is None:
        raise StreamHealthError(stage=target.stage, reason="group_missing")

    try:
        lag_result = _mapping_value(group, "lag")
        pending = _parse_redis_int(_mapping_value(group, "pending"))
        last_delivered_id = _mapping_value(group, "last-delivered-id")
    except (KeyError, TypeError, ValueError) as exc:
        raise StreamHealthError(
            stage=target.stage,
            reason="inconsistent_snapshot",
        ) from exc

    if lag_result is None:
        raise StreamHealthError(stage=target.stage, reason="lag_unknown")
    try:
        lag = _parse_redis_int(lag_result)
    except (TypeError, ValueError) as exc:
        raise StreamHealthError(
            stage=target.stage,
            reason="inconsistent_snapshot",
        ) from exc

    pending_age: float | None = None
    if pending > 0:
        if not pending_entries or not isinstance(pending_entries[0], Mapping):
            raise StreamHealthError(
                stage=target.stage,
                reason="inconsistent_snapshot",
            )
        try:
            pending_id = _mapping_value(pending_entries[0], "message_id")
            pending_age = _enqueue_age(observed_at, pending_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise StreamHealthError(
                stage=target.stage,
                reason="inconsistent_snapshot",
            ) from exc
    elif pending_entries:
        raise StreamHealthError(
            stage=target.stage,
            reason="inconsistent_snapshot",
        )

    undelivered_age: float | None = None
    if lag > 0:
        try:
            entries = await redis.xrange(
                target.stream,
                min=f"({_decoded(last_delivered_id)}",
                max="+",
                count=1,
            )
        except RedisError as exc:
            _raise_stream_health_error(target, exc)
        if not entries:
            raise _UndeliveredEntryMissing
        try:
            undelivered_age = _enqueue_age(observed_at, entries[0][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise StreamHealthError(
                stage=target.stage,
                reason="inconsistent_snapshot",
            ) from exc

    ages = [age for age in (undelivered_age, pending_age) if age is not None]
    return StreamHealthSnapshot(
        stage=target.stage,
        stream=target.stream,
        group=target.group,
        observation_timestamp=observed_at,
        retained_entries=retained,
        lag=lag,
        pending=pending,
        oldest_undelivered_enqueue_age=undelivered_age,
        oldest_pending_enqueue_age=pending_age,
        oldest_outstanding_enqueue_age=max(ages) if ages else None,
    )


async def read_stream_health(
    redis: Redis,
    target: StreamHealthTarget,
) -> StreamHealthSnapshot:
    """stage snapshotを読み、XRANGE矛盾時だけ全体を1回再読する。"""
    for attempt in range(2):
        try:
            return await _read_stream_health_once(redis, target)
        except _UndeliveredEntryMissing:
            if attempt == 1:
                raise StreamHealthError(
                    stage=target.stage,
                    reason="inconsistent_snapshot",
                ) from None
    raise AssertionError("unreachable")


async def has_idle_pending(
    redis: Redis,
    target: StreamHealthTarget,
    *,
    idle_ms: int = 600_000,
) -> bool:
    """明示診断時だけ指定idle以上のPEL entryが存在するかを1件確認する。"""
    try:
        entries = await redis.xpending_range(
            target.stream,
            target.group,
            min="-",
            max="+",
            count=1,
            idle=idle_ms,
        )
    except RedisError as exc:
        _raise_stream_health_error(target, exc)
    return bool(entries)
