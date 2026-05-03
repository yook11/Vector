"""generate_snapshot CLI — rolling 7d trends snapshot を手動投入する。

使い方::

    # 当日 (JST 0:00 上限) の rolling 7d window を生成 (= cron 相当)
    uv run python -m app.insights.snapshot.cli.generate_snapshot

    # 指定 window_end (任意の JST 日付) を生成
    uv run python -m app.insights.snapshot.cli.generate_snapshot \
        --window-end=2026-05-01

    # 既存 snapshot を上書きで再生成
    uv run python -m app.insights.snapshot.cli.generate_snapshot \
        --window-end=2026-05-01 --force

CLI は FastAPI DI とは独立した自前の ``async_sessionmaker`` を組み立てて
``WeeklyTrendsSnapshotService`` に渡す (Service の DI 形式は同じ)。

戻り値 (exit code):
- ``0``: 新規生成 / 既存スキップ (force=False で既存あり) のどちらも正常終了
- ``2``: argparse エラー (--window-end のフォーマット不正等)
- 例外発生時はトレースバックで死ぬ (feedback_failure_visibility.md)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.insights.snapshot.application.snapshot import WeeklyTrendsSnapshotService
from app.insights.snapshot.domain.ready import ReadyForDigest
from app.insights.snapshot.domain.week import latest_window_end, now_in_jst
from app.insights.snapshot.repository.snapshots import SnapshotRepository


def build_parser() -> argparse.ArgumentParser:
    """argparse パーサーを構築する。テストから直接呼べるよう独立関数にしている。"""
    parser = argparse.ArgumentParser(
        prog="generate_snapshot",
        description="Generate a rolling 7d trends snapshot.",
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
        help="既存 snapshot があっても上書きで再生成する。",
    )
    return parser


async def run(
    args: argparse.Namespace,
    service: WeeklyTrendsSnapshotService,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Service / session_factory 注入を受けて 1 回分の生成を実行する (テスト境界)。"""
    window_end = (
        args.window_end
        if args.window_end is not None
        else latest_window_end(now_in_jst())
    )

    async with session_factory() as session:
        snapshot_repo = SnapshotRepository(session)
        ready = await ReadyForDigest.try_advance_from(
            window_end=window_end,
            force=args.force,
            snapshot_repo=snapshot_repo,
        )
    if ready is None:
        print(
            f"skipped existing: window_end={window_end.isoformat()} "
            "(use --force to overwrite)"
        )
        return 0

    outcome = await service.execute(ready)
    print(
        f"generated: window_end={outcome.window_end.isoformat()} "
        f"source_analysis_count={outcome.source_analysis_count}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI エントリーポイント。

    ``-m app.insights.snapshot.cli.generate_snapshot`` から呼ばれる。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    async def _bootstrap() -> int:
        engine = create_async_engine(settings.database_url, echo=False)
        try:
            session_factory = async_sessionmaker(
                engine, class_=SQLModelAsyncSession, expire_on_commit=False
            )
            service = WeeklyTrendsSnapshotService(session_factory)
            return await run(args, service, session_factory)
        finally:
            await engine.dispose()

    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    sys.exit(main())
