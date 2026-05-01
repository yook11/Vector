"""re_extract_all CLI — 既存 article の Stage 1 一括再抽出 (Phase 1B α-1)。

使い方::

    # dry-run (default): AI 呼び出しは実行するが DB は変更しない
    uv run python -m app.analysis.extraction.cli.re_extract_all --limit 3

    # 件数限定で本実行 (production confirmation 用)
    uv run python -m app.analysis.extraction.cli.re_extract_all --execute --limit 10

    # ID 範囲を絞る (再開用)
    uv run python -m app.analysis.extraction.cli.re_extract_all \
        --execute --id-from 1000 --id-to 2000

    # 全件本実行 (Ask first 必須)
    uv run python -m app.analysis.extraction.cli.re_extract_all --execute --all

設計:

- ``--execute`` を明示しない限り dry-run (default=True): rollback で永続化を抑止
  しつつ extractor の API は実際に呼び、新 prompt の挙動を本番投入前に確認する
- ``--limit`` のみ (デフォルト 3): 小さく試走、1 回の呼び出しで API クォータを
  使い切らないための保険
- ``--all`` 指定時のみ全件 (--limit と排他)、それ以外は ``--limit`` で必ず制限
- 対象 article: 既存 ``ArticleExtraction`` を持つもの (新規は通常 pipeline 任せ)
  + ``--id-from`` / ``--id-to`` で範囲指定可 (CLI 中断後の再開用)

戻り値 (exit code):
- ``0``: 全件成功 (or 全件 skipped)
- ``2``: ``--all`` も ``--limit`` も指定なし等の argparse エラー / 引数不正
- ``3``: 再抽出途中で 1 件以上 ``failed_ids`` に入った
- 例外発生時はトレースバックで死ぬ (feedback_failure_visibility.md)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.analysis.extraction.application import (
    ReExtractionService,
    ReExtractionSummary,
)
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.gemini import GeminiExtractor
from app.config import settings
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction


def build_parser() -> argparse.ArgumentParser:
    """argparse パーサーを構築する (テストから直接呼べるよう独立関数)。"""
    parser = argparse.ArgumentParser(
        prog="re_extract_all",
        description="Re-run Stage 1 extraction for existing articles (Phase 1B α-1).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="本実行 (デフォルトは dry-run、commit せず rollback)",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="先頭 N 件のみ処理 (デフォルト 3、--all 指定時は無効)",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="範囲内 (--id-from/--id-to で絞り込み後) の全件を処理",
    )
    parser.add_argument(
        "--id-from",
        type=int,
        default=None,
        metavar="M",
        help="article_id >= M に絞る (CLI 中断後の再開用)",
    )
    parser.add_argument(
        "--id-to",
        type=int,
        default=None,
        metavar="N",
        help="article_id <= N に絞る (--id-from と組み合わせて範囲指定)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        help="1 article あたりの retry 上限 (デフォルト 3)",
    )
    return parser


async def _select_article_ids(
    session: AsyncSession,
    *,
    id_from: int | None,
    id_to: int | None,
    limit: int | None,
) -> tuple[int, ...]:
    """既存 ``ArticleExtraction`` を持つ article_id を昇順で取得する。"""
    stmt = (
        select(Article.id)
        .join(ArticleExtraction, ArticleExtraction.article_id == Article.id)
        .order_by(Article.id)
    )
    if id_from is not None:
        stmt = stmt.where(Article.id >= id_from)
    if id_to is not None:
        stmt = stmt.where(Article.id <= id_to)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return tuple(rows)


async def run(
    args: argparse.Namespace,
    service: ReExtractionService,
    extractor: BaseExtractor,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """1 回分の再抽出を実行する (テスト境界 — service / extractor / factory を注入)。"""
    # --all なし時は --limit でデフォルト 3 件に絞る (誤って全件流さない安全装置)
    effective_limit: int | None
    if args.all:
        effective_limit = None
    else:
        effective_limit = args.limit if args.limit is not None else 3

    async with session_factory() as session:
        article_ids = await _select_article_ids(
            session,
            id_from=args.id_from,
            id_to=args.id_to,
            limit=effective_limit,
        )

    if not article_ids:
        print(
            json.dumps(
                {
                    "re_extract_summary": {
                        "success": 0,
                        "failed": 0,
                        "skipped": 0,
                        "dry_run": not args.execute,
                        "note": "no_targets",
                    }
                }
            )
        )
        return 0

    summary: ReExtractionSummary = await service.execute(
        article_ids,
        extractor,
        dry_run=not args.execute,
    )
    _print_summary(summary)
    return 3 if summary.failed_ids else 0


def _print_summary(summary: ReExtractionSummary) -> None:
    """結果を 1 行 JSON で stdout に出す (CI / log 集約しやすいように)。"""
    print(
        json.dumps(
            {
                "re_extract_summary": {
                    "success": len(summary.success_ids),
                    "failed": len(summary.failed_ids),
                    "skipped": len(summary.skipped_ids),
                    "dry_run": summary.dry_run,
                    "failed_ids": list(summary.failed_ids),
                }
            }
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI エントリーポイント (``-m app.analysis.extraction.cli.re_extract_all``)。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    async def _bootstrap() -> int:
        engine = create_async_engine(settings.database_url, echo=False)
        try:
            session_factory = async_sessionmaker(
                engine, class_=SQLModelAsyncSession, expire_on_commit=False
            )
            service = ReExtractionService(session_factory, max_retries=args.max_retries)
            extractor = GeminiExtractor()
            return await run(args, service, extractor, session_factory)
        finally:
            await engine.dispose()

    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    sys.exit(main())
