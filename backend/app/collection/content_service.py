"""コンテンツ取得サービス — 記事本文と公開日時の取得・永続化を編成する。

アトミックなユースケース: ``article_id`` を受け取り、レコードを読み込んで
HTML 抽出を :class:`ArticleHtmlExtractor` に委譲し、結果を永続化する
（または恒久スキップとしてマークする）。セッション管理は内部で完結する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.html_extractor import (
    ArticleHtmlExtractor,
    PermanentFetchError,
)
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


def _needs_enrichment(article: NewsArticle) -> bool:
    """記事が HTML エンリッチメントを必要とするか判定する。"""
    return article.original_content is None or article.published_at is None


@dataclass(frozen=True)
class ContentFetchResult:
    """コンテンツ取得ユースケースの結果。"""

    status: Literal["fetched", "already_exists", "skipped"]


class ContentFetchService:
    """1 記事の本文・公開日時取得と永続化を行うアトミックなユースケース。

    責務を明確に分離している:
      1. 記事レコードの読み込み（DB）。
      2. HTML からの本文・公開日時抽出（``ArticleHtmlExtractor`` へ委譲）。
      3. 結果の永続化: content と published_at を独立に判断。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        html_extractor: ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._html_extractor = html_extractor

    async def execute(self, article_id: int) -> ContentFetchResult:
        """記事本文と公開日時を取得し永続化する。

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
            if not _needs_enrichment(article):
                return ContentFetchResult("already_exists")

            # 2. HTML 抽出を委譲
            try:
                extraction = await self._html_extractor.fetch(str(article.original_url))
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

            # 3. 結果を独立に適用
            updated = False

            # 本文: 未取得かつ抽出成功の場合のみ保存
            if article.original_content is None and extraction.body is not None:
                article.original_content = extraction.body
                updated = True

            # 公開日時: 未取得かつ抽出成功の場合のみ保存
            if article.published_at is None and extraction.published_at is not None:
                article.published_at = extraction.published_at
                updated = True

            # 本文も日時も取れなかった場合は恒久スキップ
            if not updated and extraction.body is None:
                article.skip_content_fetch = True
                session.add(article)
                await session.commit()
                logger.info(
                    "content_fetch_skip",
                    article_id=article_id,
                    reason="quality_gate",
                )
                return ContentFetchResult("skipped")

            session.add(article)
            await session.commit()
            logger.info(
                "content_fetch_completed",
                article_id=article_id,
                body_extracted=extraction.body is not None,
                date_extracted=extraction.published_at is not None,
            )
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
