"""トレンド生成をTaskiqの定期タスクとして登録する。"""

from __future__ import annotations

from taskiq import AsyncBroker, AsyncTaskiqDecoratedTask, Context, TaskiqDepends

from app.audit.domain.event import Stage
from app.config import settings
from app.insights.trend_discovery.service import TrendDiscoveryService
from app.logfire.stage_span import pipeline_stage_span
from app.queue.schedule import CRON_TREND_DISCOVERY
from app.shared.revalidate import FrontendRevalidateNotifier


async def run_trend_discovery(ctx: Context = TaskiqDepends()) -> None:
    """ワーカーのセッションと通知設定を使ってトレンドを生成する。"""
    with pipeline_stage_span(Stage.TREND_DISCOVERY, op="run_trend_discovery"):
        service = TrendDiscoveryService(ctx.state.session_factory)
        notifier = FrontendRevalidateNotifier.from_settings(settings)
        await service.create(notifier)


def register_trend_discovery_task(
    broker: AsyncBroker,
) -> AsyncTaskiqDecoratedTask:
    """トレンド生成タスクを指定されたブローカーへ登録する。"""
    return broker.register_task(
        run_trend_discovery,
        task_name="run_trend_discovery",
        timeout=600,
        max_retries=0,
        retry_on_error=False,
        schedule=[{"cron": CRON_TREND_DISCOVERY}],
    )
