"""pipeline health 観測の集計サービス。

観測時刻 ``observed_at`` を 1 度だけ確定し、event 窓 (24h rolling) と backfill 窓
(``BackfillWindow``: 7day/30min) の両方の計算で共有する。``observed_at`` は
テスト決定性のため注入可能 (default は ``utc_now()``)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.admin.pipeline_health.repository import PipelineHealthRepository
from app.admin.pipeline_health.schemas import (
    PipelineHealthResponse,
    PipelineHealthSummary,
    PipelineStageHealth,
)
from app.audit.domain.event import EventType, Stage
from app.queue.helpers.window import BackfillWindow
from app.shared.time import utc_now

_EVENT_WINDOW = timedelta(hours=24)


def _age_seconds(observed_at: datetime, ts: datetime | None) -> int | None:
    """``observed_at`` から ``ts`` までの経過秒。``ts`` が無ければ ``None``。"""
    if ts is None:
        return None
    return int((observed_at - ts).total_seconds())


class PipelineHealthService:
    def __init__(self, repo: PipelineHealthRepository) -> None:
        self._repo = repo

    async def get_health(
        self, observed_at: datetime | None = None
    ) -> PipelineHealthResponse:
        observed_at = observed_at or utc_now()
        event_window_start = observed_at - _EVENT_WINDOW
        created_before, created_after = BackfillWindow().boundaries_at(observed_at)

        event_counts = await self._repo.event_counts_24h(
            event_window_start=event_window_start
        )
        last_succeeded = await self._repo.last_succeeded_at()
        completion_count, completion_oldest = await self._repo.completion_queue()
        backfill = await self._repo.backfill_stats(
            created_before=created_before, created_after=created_after
        )

        stages: list[PipelineStageHealth] = []
        for stage in Stage:
            if stage is Stage.COMPLETION:
                queue_count, queue_oldest = completion_count, completion_oldest
            else:
                queue_count, queue_oldest = 0, None
            backfill_count, backfill_oldest = backfill.get(stage, (0, None))
            stages.append(
                PipelineStageHealth(
                    stage=stage,
                    # events/last_succeeded は全 stage 共通軸 (欠落=真の 0/未成功)。
                    succeeded_event_count_24h=event_counts.get(
                        (stage, EventType.SUCCEEDED), 0
                    ),
                    failed_event_count_24h=event_counts.get(
                        (stage, EventType.FAILED), 0
                    ),
                    queue_count=queue_count,
                    oldest_queue_age_seconds=_age_seconds(observed_at, queue_oldest),
                    backfill_target_count=backfill_count,
                    oldest_backfill_target_age_seconds=_age_seconds(
                        observed_at, backfill_oldest
                    ),
                    last_succeeded_at=last_succeeded.get(stage),
                )
            )

        return PipelineHealthResponse(
            summary=self._build_summary(
                observed_at=observed_at,
                event_window_start=event_window_start,
                stages=stages,
                completion_count=completion_count,
                completion_oldest=completion_oldest,
            ),
            stages=stages,
        )

    def _build_summary(
        self,
        *,
        observed_at: datetime,
        event_window_start: datetime,
        stages: list[PipelineStageHealth],
        completion_count: int,
        completion_oldest: datetime | None,
    ) -> PipelineHealthSummary:
        # backfill 補助メトリクスを持たない stage は 0 のため、全 stage 合算は
        # curation/assessment/embedding の backfill 合計に一致する。
        backfill_ages = [
            s.oldest_backfill_target_age_seconds
            for s in stages
            if s.oldest_backfill_target_age_seconds is not None
        ]
        return PipelineHealthSummary(
            failed_event_count_24h=sum(s.failed_event_count_24h for s in stages),
            backfill_target_total=sum(s.backfill_target_count for s in stages),
            # 最大 age = 全 stage 中の最古 target。対象 0 なら None。
            oldest_backfill_target_age_seconds=(
                max(backfill_ages) if backfill_ages else None
            ),
            completion_queue_count=completion_count,
            oldest_completion_queue_age_seconds=_age_seconds(
                observed_at, completion_oldest
            ),
            observed_at=observed_at,
            event_window_start=event_window_start,
        )
