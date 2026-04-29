"""generate_snapshot CLI — 週次トレンド snapshot を手動投入する。

使い方::

    # 直近完了週を生成 (= cron 相当)
    uv run python -m app.digest.cli.generate_snapshot

    # 指定週 (JST 月曜開始日) を生成
    uv run python -m app.digest.cli.generate_snapshot --week=2026-04-13

    # 既存 snapshot を上書きで再生成
    uv run python -m app.digest.cli.generate_snapshot --week=2026-04-13 --force

CLI は FastAPI DI とは独立した自前の ``async_sessionmaker`` を組み立てて
``WeeklyTrendsSnapshotService`` に渡す (Service の DI 形式は同じ)。

戻り値 (exit code):
- ``0``: 新規生成 / 既存スキップ (force=False で既存あり) のどちらも正常終了
- ``2``: argparse エラー (--week のフォーマット不正等)
- 例外発生時はトレースバックで死ぬ (``--week`` が月曜以外なら ``ReadyForDigest``
  構築時の ``ValidationError`` が伝播する: feedback_failure_visibility.md)
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
from app.digest.application.snapshot import WeeklyTrendsSnapshotService
from app.digest.domain.ready import ReadyForDigest
from app.digest.domain.week import latest_completed_week_start, now_in_jst
from app.digest.repository.snapshots import SnapshotRepository


def build_parser() -> argparse.ArgumentParser:
    """argparse パーサーを構築する。テストから直接呼べるよう独立関数にしている。"""
    parser = argparse.ArgumentParser(
        prog="generate_snapshot",
        description="Generate a weekly trends snapshot for the digest.",
    )
    parser.add_argument(
        "--week",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="JST 月曜開始日 (YYYY-MM-DD)。省略時は直近完了週を自動算出する。",
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
    week_start = (
        args.week
        if args.week is not None
        else latest_completed_week_start(now_in_jst())
    )

    async with session_factory() as session:
        snapshot_repo = SnapshotRepository(session)
        ready = await ReadyForDigest.try_advance_from(
            week_start=week_start,
            force=args.force,
            snapshot_repo=snapshot_repo,
        )
    if ready is None:
        print(
            f"skipped existing: week_start={week_start.isoformat()} "
            "(use --force to overwrite)"
        )
        return 0

    outcome = await service.execute(ready)
    print(
        f"generated: week_start={outcome.week_start.isoformat()} "
        f"source_analysis_count={outcome.source_analysis_count}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI エントリーポイント。``-m app.digest.cli.generate_snapshot`` から呼ばれる。"""
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
