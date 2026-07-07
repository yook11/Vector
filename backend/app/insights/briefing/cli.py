"""generate_briefing CLI — 週次カテゴリ別 LLM ブリーフィングを手動投入する。

使い方::

    # 直近完了週 × 全カテゴリを順次生成 (= cron 相当を CLI 1 プロセスで実行)
    uv run python -m app.insights.briefing.cli

    # 指定週 × 単一カテゴリを生成
    uv run python -m app.insights.briefing.cli \\
        --week=2026-04-20 --category=ai

    # 既存 briefing を上書き再生成
    uv run python -m app.insights.briefing.cli \\
        --week=2026-04-20 --category=ai --force

CLI は dispatcher と異なり 1 プロセスで Service.execute を順次回す
(taskiq 経由の per-category subtask に分けない)。手動運用 / 復旧経路。

戻り値 (exit code):
- ``0``: 全カテゴリ正常終了 (生成 / skip 含む)
- ``2``: argparse エラー
- 例外発生時はトレースバックで死ぬ (`feedback_failure_visibility.md`)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.config import settings
from app.db_ssl import create_app_engine
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.insights.briefing.llm import DeepSeekBriefingGenerator
from app.insights.briefing.repository import BriefingRepository
from app.insights.briefing.service import BriefingConflict, WeeklyBriefingService
from app.models.category import Category
from app.shared.revalidate import NullRevalidateNotifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_briefing",
        description="Generate weekly category briefings via DeepSeek LLM.",
    )
    parser.add_argument(
        "--week",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="JST 月曜開始日 (YYYY-MM-DD)。省略時は直近完了週を自動算出する。",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        metavar="SLUG",
        help="カテゴリ slug (例: ai)。省略時は全カテゴリを順次処理する。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存 briefing があっても上書きで再生成する。",
    )
    return parser


async def _resolve_categories(
    session: AsyncSession, slug: str | None
) -> list[Category]:
    if slug is None:
        rows = await session.execute(select(Category).order_by(Category.id))
        return list(rows.scalars().all())
    row = await session.execute(select(Category).where(Category.slug == slug))
    cat = row.scalar_one_or_none()
    if cat is None:
        raise SystemExit(f"category not found: slug={slug}")
    return [cat]


async def run(
    args: argparse.Namespace,
    service: WeeklyBriefingService,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    week_start = (
        args.week
        if args.week is not None
        else latest_completed_week_start(now_in_jst())
    )
    async with session_factory() as session:
        categories = await _resolve_categories(session, args.category)

    for category in categories:
        async with session_factory() as session:
            repo = BriefingRepository(session)
            ready = await ReadyForBriefing.try_advance_from(
                week_start=week_start,
                category_id=category.id,
                force=args.force,
                briefing_repo=repo,
            )
        if ready is None:
            # 既存 briefing あり = benign な冪等 skip。監査に焼かず stdout で観測する。
            print(
                f"skipped existing: week={week_start.isoformat()} "
                f"category={category.slug} (use --force to overwrite)"
            )
            continue

        outcome = await service.execute(ready)
        if isinstance(outcome, BriefingConflict):
            print(
                f"skipped conflict: week={week_start.isoformat()} "
                f"category={category.slug} (another worker saved first)"
            )
        elif outcome.persisted:
            print(
                f"generated: week={outcome.week_start.isoformat()} "
                f"category={category.slug} article_count={outcome.article_count}"
            )
        else:
            print(
                f"skipped no_articles: week={week_start.isoformat()} "
                f"category={category.slug}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    async def _bootstrap() -> int:
        engine = create_app_engine(
            settings.database_url,
            application_name="vector-cli-generate-briefing",
            echo=False,
        )
        try:
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            # CLI は手動運用 / 復旧経路のため、frontend revalidate は飛ばす
            # (本番 cron 経路と異なり 11 連続 POST が即時走るのを避ける)。
            service = WeeklyBriefingService(
                session_factory,
                DeepSeekBriefingGenerator(),
                NullRevalidateNotifier(),
            )
            return await run(args, service, session_factory)
        finally:
            await engine.dispose()

    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    sys.exit(main())
