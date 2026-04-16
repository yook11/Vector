"""コンテンツ取得サービス — 記事本文の取得と永続化を編成する。

アトミックなユースケース: ``article_id`` を受け取り、レコードを読み込んで
本文取得を :class:`ArticleBodyFetcher` に委譲し、結果を永続化する
（または恒久スキップとしてマークする）。セッション管理は内部で完結する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_body_fetcher import (
    ArticleBodyFetcher,
    PermanentFetchError,
)
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ContentFetchResult:
    """コンテンツ取得ユースケースの結果。"""

    status: Literal["fetched", "already_exists", "skipped"]


class ContentFetchService:
    """1 記事の本文取得と永続化を行うアトミックなユースケース。

    責務を明確に分離している:
      1. 記事レコードの読み込み（DB）。
      2. 本文テキストの取得（``ArticleBodyFetcher`` へ委譲）。
      3. 本文の永続化、または恒久スキップとしてマーク。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        body_fetcher: ArticleBodyFetcher,
    ) -> None:
        self._session_factory = session_factory
        self._body_fetcher = body_fetcher

    async def execute(self, article_id: int) -> ContentFetchResult:
        """記事本文を取得し永続化する。

        Returns:
            status を持つ ContentFetchResult。

        Raises:
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            # 1. 記事レコードを読み込む
            article = await session.get(NewsArticle, article_id)
            if article is None:
                logger.warning("content_fetch_article_not_found", article_id=article_id)
                return ContentFetchResult("skipped")
            if article.original_content is not None:
                return ContentFetchResult("already_exists")

            # 2. 本文取得を委譲
            try:
                content = await self._body_fetcher.fetch(str(article.original_url))
            except PermanentFetchError as e:
                article.skip_content_fetch = True
                session.add(article)
                await session.commit()
                logger.info(
                    "content_fetch_skip",
                    article_id=article_id,
                    reason=str(e),
                )
                return ContentFetchResult("skipped")

            # 品質ゲートで棄却された場合は恒久スキップ
            if content is None:
                article.skip_content_fetch = True
                session.add(article)
                await session.commit()
                logger.info(
                    "content_fetch_skip",
                    article_id=article_id,
                    reason="quality_gate",
                )
                return ContentFetchResult("skipped")

            # 3. 永続化
            article.original_content = content
            session.add(article)
            await session.commit()
            logger.info("content_fetch_completed", article_id=article_id)
            return ContentFetchResult("fetched")


async def mark_article_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    article_id: int,
) -> None:
    """記事を恒久スキップとしてマークする（Task の最終試行用）。"""
    async with session_factory() as session:
        article = await session.get(NewsArticle, article_id)
        if article is not None:
            article.skip_content_fetch = True
            session.add(article)
            await session.commit()
