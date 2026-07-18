"""acquisition / completion / curation / assessment Stream health の毎分sampler。"""

from __future__ import annotations

import logfire
import structlog

from app.queue.brokers import broker_maintenance
from app.queue.schedule import CRON_PIPELINE_QUEUE_HEALTH
from app.queue.stream_health import (
    PIPELINE_QUEUE_TARGETS,
    StreamHealthError,
    StreamHealthSnapshot,
    read_stream_health,
)
from app.redis import get_redis

logger = structlog.get_logger(__name__)

_retained_entries_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.retained_entries",
    unit="1",
    description="Redis Streamが保持するACK済み履歴を含むentry数",
)
_lag_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.lag",
    unit="1",
    description="consumer groupへ未配達のentry数",
)
_pending_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.pending",
    unit="1",
    description="consumer groupで配達済みかつ未ACKのentry数",
)
_oldest_undelivered_enqueue_age_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.oldest_undelivered_enqueue_age",
    unit="s",
    description="最古の未配達entryがenqueueされてからの秒数",
)
_oldest_pending_enqueue_age_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.oldest_pending_enqueue_age",
    unit="s",
    description="PEL最小IDのentryがenqueueされてからの秒数",
)
_oldest_outstanding_enqueue_age_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.oldest_outstanding_enqueue_age",
    unit="s",
    description="未配達とpendingのうち最古のenqueue age秒数",
)
_observation_up_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.observation_up",
    unit="1",
    description="stage別Stream snapshotの観測成功状態",
)
_observation_timestamp_gauge = logfire.metric_gauge(
    "vector.pipeline.queue.observation_timestamp",
    unit="s",
    description="snapshotに使ったRedis TIMEのUnix timestamp",
)


def _age_or_zero(age: float | None) -> float:
    """正常snapshotに対象entryがないageだけを0へ変換する。"""
    return 0.0 if age is None else age


def _record_success(snapshot: StreamHealthSnapshot) -> None:
    """完全なsnapshotだけをstage属性付きgaugesへ記録する。"""
    attributes = {"stage": snapshot.stage}
    _retained_entries_gauge.set(
        snapshot.retained_entries,
        attributes=attributes,
    )
    _lag_gauge.set(snapshot.lag, attributes=attributes)
    _pending_gauge.set(snapshot.pending, attributes=attributes)
    _oldest_undelivered_enqueue_age_gauge.set(
        _age_or_zero(snapshot.oldest_undelivered_enqueue_age),
        attributes=attributes,
    )
    _oldest_pending_enqueue_age_gauge.set(
        _age_or_zero(snapshot.oldest_pending_enqueue_age),
        attributes=attributes,
    )
    _oldest_outstanding_enqueue_age_gauge.set(
        _age_or_zero(snapshot.oldest_outstanding_enqueue_age),
        attributes=attributes,
    )
    _observation_up_gauge.set(1, attributes=attributes)
    _observation_timestamp_gauge.set(
        snapshot.observation_timestamp,
        attributes=attributes,
    )


@broker_maintenance.task(
    task_name="observe_pipeline_queue_health",
    timeout=15,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_PIPELINE_QUEUE_HEALTH}],
)
async def observe_pipeline_queue_health() -> None:
    """固定4stageのsnapshotを独立して取得しLogfireへ記録する。"""
    redis = get_redis()
    for target in PIPELINE_QUEUE_TARGETS:
        try:
            snapshot = await read_stream_health(redis, target)
        except StreamHealthError as error:
            _observation_up_gauge.set(0, attributes={"stage": target.stage})
            logger.warning(
                "pipeline_queue_health_observation_failed",
                stage=target.stage,
                reason=error.reason,
            )
            continue
        _record_success(snapshot)
