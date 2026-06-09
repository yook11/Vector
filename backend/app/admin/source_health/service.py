"""ソース別 health 観測の集計サービス。

観測時刻 ``observed_at`` を 1 度だけ確定し、選択窓 (``window_hours``) 内の event を
窓依存指標 (analyzable / processed / failure reasons) に、incomplete count と
last succeeded at を窓非依存の現在値に使う。``observed_at`` はテスト決定性のため
注入可能 (default は ``utc_now()``)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.admin.source_health.repository import SourceHealthRepository
from app.admin.source_health.schemas import (
    FailureReason,
    SourceHealthItem,
    SourceHealthResponse,
)
from app.audit.domain.event import EventType
from app.models.news_source import NewsSource
from app.shared.time import utc_now


@dataclass(frozen=True, slots=True)
class _WindowMetrics:
    """1 source の窓依存指標。"""

    analyzable: int
    processed: int
    rate: float | None
    failure_reasons: list[FailureReason]


def _to_metrics(rows: list[tuple[str, str, int]]) -> _WindowMetrics:
    """repository が絞った ``(event_type, outcome_code, count)`` を指標に変換する。

    succeeded はすべて analyzable (repository が analyzable 2 種のみ返す)。非対称:
    ``rejected`` は processed と failure reasons の両方、``failed`` は failure
    reasons のみ (記事候補単位とは限らないため processed に入れない)。
    """
    analyzable = 0
    rejected_total = 0
    failure_by_code: dict[str, int] = defaultdict(int)

    for event_type, outcome_code, count in rows:
        if event_type == EventType.SUCCEEDED:
            analyzable += count
        elif event_type == EventType.REJECTED:
            rejected_total += count
            failure_by_code[outcome_code] += count
        elif event_type == EventType.FAILED:
            failure_by_code[outcome_code] += count

    processed = analyzable + rejected_total
    rate = round(analyzable / processed * 100, 1) if processed > 0 else None
    failure_reasons = [
        FailureReason(outcome_code=code, count=count)
        # count 降順、同数は outcomeCode 昇順。
        for code, count in sorted(
            failure_by_code.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]
    return _WindowMetrics(analyzable, processed, rate, failure_reasons)


class SourceHealthService:
    def __init__(self, repo: SourceHealthRepository) -> None:
        self._repo = repo

    async def get_health(
        self, *, window_hours: int, observed_at: datetime | None = None
    ) -> SourceHealthResponse:
        observed_at = observed_at or utc_now()
        window_start = observed_at - timedelta(hours=window_hours)

        sources = await self._repo.all_sources()
        event_rows = await self._repo.windowed_metric_event_counts(
            window_start=window_start
        )
        incomplete = await self._repo.incomplete_counts()
        last_succeeded = await self._repo.last_succeeded_at()

        by_source: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
        for source_id, event_type, outcome_code, count in event_rows:
            by_source[source_id].append((event_type, outcome_code, count))

        items = [
            self._build_item(
                source=source,
                metrics=_to_metrics(by_source.get(source.id, [])),
                incomplete_count=incomplete.get(source.id, 0),
                last_succeeded_at=last_succeeded.get(source.id),
            )
            for source in sources  # all_sources は name 昇順
        ]
        return SourceHealthResponse(
            window_hours=window_hours, observed_at=observed_at, items=items
        )

    @staticmethod
    def _build_item(
        *,
        source: NewsSource,
        metrics: _WindowMetrics,
        incomplete_count: int,
        last_succeeded_at: datetime | None,
    ) -> SourceHealthItem:
        return SourceHealthItem(
            source_id=source.id,
            source_name=str(source.name),
            source_type=source.source_type,
            is_active=source.is_active,
            analyzable_rate=metrics.rate,
            analyzable_count=metrics.analyzable,
            processed_article_count=metrics.processed,
            incomplete_count=incomplete_count,
            failure_reasons=metrics.failure_reasons,
            last_succeeded_at=last_succeeded_at,
        )
