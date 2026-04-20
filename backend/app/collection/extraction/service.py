"""コンテンツ取得サービス — 記事本文と公開日時の取得・Article 行の作成を編成する。

アトミックなユースケース: ``discovered_article_id`` を受け取り、
DiscoveredArticle からURL を取得して HTML 抽出を
:class:`ArticleHtmlExtractor` に委譲し、品質を満たせば Article 行を作成する。
セッション管理は内部で完結する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.collection.errors import PermanentFetchError
from app.collection.extraction.extractor import ArticleHtmlExtractor
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ContentFetchResult:
    """コンテンツ取得ユースケースの結果。"""

    status: Literal["fetched", "already_exists", "skipped"]
    article_id: int | None = None


class ContentFetchService:
    """1 記事の本文・公開日時取得と Article 行作成を行うアトミックなユースケース。

    責務を明確に分離している:
      1. DiscoveredArticle の読み込み（DB）。
      2. HTML からの本文・公開日時抽出（``ArticleHtmlExtractor`` へ委譲）。
      3. 品質を満たす場合に Article 行を作成。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        html_extractor: ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._html_extractor = html_extractor

    async def execute(self, discovered_article_id: int) -> ContentFetchResult:
        """記事本文と公開日時を取得し Article 行を作成する。

        Returns:
            status と article_id を持つ ContentFetchResult。

        Raises:
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            # 1. DiscoveredArticle を読み込む
            discovered = await session.get(
                DiscoveredArticle,
                discovered_article_id,
                options=[selectinload(DiscoveredArticle.article)],
            )
            if discovered is None:
                logger.warning(
                    "content_fetch_discovered_not_found",
                    discovered_article_id=discovered_article_id,
                )
                return ContentFetchResult("skipped")

            # 冪等性チェック: 既に Article が存在する場合
            if discovered.article is not None:
                return ContentFetchResult(
                    "already_exists", article_id=discovered.article.id
                )

            # 2. HTML 抽出を委譲
            try:
                extraction = await self._html_extractor.fetch(
                    str(discovered.original_url)
                )
            except PermanentFetchError as e:
                logger.info(
                    "content_fetch_skip",
                    discovered_article_id=discovered_article_id,
                    reason=str(e),
                )
                return ContentFetchResult("skipped")

            # 3. 品質チェック: 本文が取れなければスキップ
            if extraction.body is None:
                logger.info(
                    "content_fetch_skip",
                    discovered_article_id=discovered_article_id,
                    reason="quality_gate",
                )
                return ContentFetchResult("skipped")

            # 4. Article 行を作成
            article = Article(
                discovered_article_id=discovered.id,
                original_title=discovered.original_title,
                original_content=extraction.body,
                published_at=extraction.published_at,
            )
            session.add(article)
            await session.commit()
            await session.refresh(article)

            logger.info(
                "content_fetch_completed",
                discovered_article_id=discovered_article_id,
                article_id=article.id,
                date_extracted=extraction.published_at is not None,
            )
            return ContentFetchResult("fetched", article_id=article.id)
