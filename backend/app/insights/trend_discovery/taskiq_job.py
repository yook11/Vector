"""Trend Discovery ServiceをTaskiqへ登録するcron job。

スケジュール:
- ``CRON_TREND_DISCOVERY`` (UTC) = JST 毎日 00:05 — 直近完了 7 日窓
  (``[今日0:00 - 7d, 今日0:00)`` JST) を集計し、
  集計対象 analysis がある場合のみ ``trends_snapshots`` に 1 行 INSERT する

責務:
- Taskiq Contextからworker所有のsession factoryを取り出す
- ``TrendDiscoveryService.create()`` を呼ぶ
- task名、cron、timeout、retryをbrokerへ登録する

エラー方針 (feedback_failure_visibility.md):
- 例外は捕まえずに伝播させる (taskiq 側の retry/log に委ねる)
- 既存 snapshot あり (Ready が None) は正常終了として扱う
- 集計対象記事 0 件は Service の正常 skip として扱う
"""

from __future__ import annotations

from taskiq import AsyncBroker, AsyncTaskiqDecoratedTask, Context, TaskiqDepends

from app.audit.domain.event import Stage
from app.config import settings
from app.insights.trend_discovery.service import TrendDiscoveryService
from app.logfire.stage_span import pipeline_stage_span
from app.queue.schedule import CRON_TREND_DISCOVERY
from app.shared.revalidate import FrontendRevalidateNotifier


async def run_trend_discovery(ctx: Context = TaskiqDepends()) -> None:
    """workerのresourceを使ってTrend Discoveryを作成する。"""
    with pipeline_stage_span(Stage.TREND_DISCOVERY, op="run_trend_discovery"):
        service = TrendDiscoveryService(ctx.state.session_factory)
        notifier = FrontendRevalidateNotifier.from_settings(settings)
        await service.create(notifier)


def register_trend_discovery_task(
    broker: AsyncBroker,
) -> AsyncTaskiqDecoratedTask:
    """Trend Discovery taskを指定されたbrokerへ登録する。"""
    return broker.register_task(
        run_trend_discovery,
        task_name="run_trend_discovery",
        timeout=600,
        max_retries=0,
        retry_on_error=False,
        schedule=[{"cron": CRON_TREND_DISCOVERY}],
    )
