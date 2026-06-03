"""run_trend_discovery CLI — rolling 7d Trend Discovery を手動実行する。

使い方::

    # 当日 (JST 0:00 上限) の rolling 7d window を実行 (= cron 相当)
    uv run python -m app.insights.trend_discovery.cli.run_trend_discovery

    # 指定 window_end (任意の JST 日付) を実行
    uv run python -m app.insights.trend_discovery.cli.run_trend_discovery \
        --window-end=2026-05-01

    # 既存 snapshot を上書きで再実行
    uv run python -m app.insights.trend_discovery.cli.run_trend_discovery \
        --window-end=2026-05-01 --force

CLI は FastAPI DI とは独立した自前の ``async_sessionmaker`` を組み立てて
``TrendDiscoveryService`` に渡す (Service の DI 形式は同じ)。

戻り値 (exit code):
- ``0``: 完了 / 既存スキップ / 集計対象記事 0 件 skip は正常終了
- ``2``: argparse エラー (--window-end のフォーマット不正等)
- 例外発生時はトレースバックで死ぬ (feedback_failure_visibility.md)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.audit.domain.event import EventType
from app.audit.stages.trend_discovery import (
    TrendDiscoveryOutcomeCode,
    append_trend_discovery_run_event_best_effort,
)
from app.config import settings
from app.db_ssl import create_app_engine
from app.insights.trend_discovery.application.service import (
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.window import latest_window_end, now_in_jst
from app.insights.trend_discovery.repository.snapshots import SnapshotRepository

_WINDOW = timedelta(days=7)


def build_parser() -> argparse.ArgumentParser:
    """argparse パーサーを構築する。テストから直接呼べるよう独立関数にしている。"""
    parser = argparse.ArgumentParser(
        prog="run_trend_discovery",
        description="Run rolling 7d Trend Discovery.",
    )
    parser.add_argument(
        "--window-end",
        dest="window_end",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "JST 集計対象日 (YYYY-MM-DD)。半開区間 [window_end - 7d, window_end) "
            "の上端。省略時は当日を自動算出する。"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存 snapshot があっても上書きで再実行する。",
    )
    return parser


async def run(
    args: argparse.Namespace,
    service: TrendDiscoveryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Service / session_factory 注入を受けて 1 回分の生成を実行する (テスト境界)。"""
    window_end = (
        args.window_end
        if args.window_end is not None
        else latest_window_end(now_in_jst())
    )
    window_start = _window_start(window_end)

    try:
        async with session_factory() as session:
            snapshot_repo = SnapshotRepository(session)
            ready = await ReadyForTrendDiscovery.try_advance_from(
                window_end=window_end,
                force=args.force,
                snapshot_repo=snapshot_repo,
            )
    except Exception as exc:
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.FAILED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
            window_start=window_start,
            window_end=window_end,
            trigger="cli",
            requested_update=args.force,
            exc=exc,
        )
        raise

    if ready is None:
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.SKIPPED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_ALREADY_EXISTS,
            window_start=window_start,
            window_end=window_end,
            trigger="cli",
            requested_update=args.force,
        )
        print(
            f"skipped existing: window_end={window_end.isoformat()} "
            "(use --force to overwrite)"
        )
        return 0

    try:
        outcome = await service.execute(ready)
    except Exception as exc:
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.FAILED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
            window_start=window_start,
            window_end=window_end,
            trigger="cli",
            requested_update=args.force,
            exc=exc,
        )
        raise

    if isinstance(outcome, SkippedNoTargetArticles):
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.SKIPPED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_NO_TARGET_ARTICLES,
            window_start=window_start,
            window_end=outcome.window_end,
            trigger="cli",
            requested_update=args.force,
            source_analysis_count=outcome.source_analysis_count,
            completed_category_count=outcome.completed_category_count,
        )
        print(
            f"skipped no target articles: window_end={outcome.window_end.isoformat()}"
        )
        return 0
    if isinstance(outcome, TrendDiscoveryConflict):
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.SKIPPED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_CONFLICT,
            window_start=window_start,
            window_end=outcome.window_end,
            trigger="cli",
            requested_update=args.force,
            source_analysis_count=outcome.source_analysis_count,
            completed_category_count=outcome.completed_category_count,
        )
        print(f"skipped conflict: window_end={outcome.window_end.isoformat()}")
        return 0

    outcome_code = (
        TrendDiscoveryOutcomeCode.RUN_UPDATED
        if isinstance(outcome, TrendDiscoveryCompleted) and outcome.updated
        else TrendDiscoveryOutcomeCode.RUN_COMPLETED
    )
    await append_trend_discovery_run_event_best_effort(
        session_factory,
        event_type=EventType.SUCCEEDED,
        outcome_code=outcome_code,
        window_start=window_start,
        window_end=outcome.window_end,
        trigger="cli",
        requested_update=args.force,
        source_analysis_count=outcome.source_analysis_count,
        completed_category_count=outcome.completed_category_count,
    )
    action = "updated" if outcome.updated else "completed"
    print(
        f"{action}: window_end={outcome.window_end.isoformat()} "
        f"source_analysis_count={outcome.source_analysis_count} "
        f"completed_category_count={outcome.completed_category_count}"
    )
    return 0


def _window_start(window_end: date) -> date:
    return window_end - _WINDOW


def main(argv: Sequence[str] | None = None) -> int:
    """CLI エントリーポイント。

    ``-m app.insights.trend_discovery.cli.run_trend_discovery`` から呼ばれる。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    async def _bootstrap() -> int:
        engine = create_app_engine(
            settings.database_url,
            application_name="vector-cli-run-trend-discovery",
            echo=False,
        )
        try:
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            service = TrendDiscoveryService(session_factory)
            return await run(args, service, session_factory)
        finally:
            await engine.dispose()

    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    sys.exit(main())
