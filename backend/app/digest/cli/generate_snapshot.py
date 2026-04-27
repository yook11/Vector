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
- ``0``: Generated / Skipped (どちらも正常終了)
- ``2``: argparse エラー (--week のフォーマット不正等)
- 例外発生時はトレースバックで死ぬ (例外は捕まえない:
  feedback_failure_visibility.md)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.digest.application.snapshot import (
    Generated,
    Skipped,
    WeeklyTrendsSnapshotService,
)


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


async def run(args: argparse.Namespace, service: WeeklyTrendsSnapshotService) -> int:
    """Service 注入を受けて 1 回分の生成を実行する (テスト境界)。"""
    if args.week is None:
        outcome = await service.generate_for_latest_completed_week(force=args.force)
    else:
        outcome = await service.generate_for_week(args.week, force=args.force)
    match outcome:
        case Generated(week_start=ws, source_analysis_count=n):
            print(f"generated: week_start={ws.isoformat()} source_analysis_count={n}")
            return 0
        case Skipped(week_start=ws):
            print(
                f"skipped existing: week_start={ws.isoformat()} "
                "(use --force to overwrite)"
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
            return await run(args, service)
        finally:
            await engine.dispose()

    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    sys.exit(main())
