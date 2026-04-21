"""コンテンツ取得サービス — 記事本文と公開日時の取得・Article 行の作成を編成する。

アトミックなユースケース: ``discovered_article_id`` を受け取り、
DB ルックアップ (:class:`DiscoveredArticleRepository`) の sum type 結果で分岐し、
未抽出ケースでは HTML 抽出を :class:`ArticleHtmlExtractor` に委譲し、
成功ケース (:class:`ExtractedContent`) では
:class:`ArticleRepository` 経由で Article 行を作成する。
セッション管理は内部で完結する。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError
from app.collection.extraction.candidate import (
    AlreadyExtracted,
    DiscoveredNotFound,
    UnextractedDiscoveredArticle,
    UnextractedFound,
)
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleRepository,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Fetched:
    """コンテンツ取得結果: HTML から抽出して Article 行を新規作成した。"""

    article_id: int


@dataclass(frozen=True)
class AlreadyExists:
    """コンテンツ取得結果: Article が既に存在しており冪等にスキップした。"""

    article_id: int


@dataclass(frozen=True)
class Skipped:
    """コンテンツ取得結果: 抽出をスキップした。

    品質ゲート未達 / 永続的失敗 / DiscoveredArticle 不在 のいずれか。
    """


ContentFetchResult = Fetched | AlreadyExists | Skipped


class ContentFetchService:
    """1 記事の本文・公開日時取得と Article 行作成を行うアトミックなユースケース。

    責務を明確に分離している:
      1. DiscoveredArticle の状態ルックアップ（Repository）。
      2. HTML からの本文・公開日時抽出（``ArticleHtmlExtractor`` へ委譲）。
      3. 品質ゲート通過時の Article 行作成（Repository 経由）。
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
            Fetched / AlreadyExists / Skipped のいずれか。

        Raises:
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            discovered_repo = DiscoveredArticleRepository(session)
            article_repo = ArticleRepository(session)

            match await discovered_repo.lookup_for_extraction(discovered_article_id):
                case DiscoveredNotFound():
                    logger.warning(
                        "content_fetch_discovered_not_found",
                        discovered_article_id=discovered_article_id,
                    )
                    return Skipped()
                case AlreadyExtracted(article_id=article_id):
                    return AlreadyExists(article_id=article_id)
                case UnextractedFound(article=unextracted):
                    return await self._fetch_and_persist(
                        session, article_repo, unextracted
                    )

    async def _fetch_and_persist(
        self,
        session: AsyncSession,
        article_repo: ArticleRepository,
        unextracted: UnextractedDiscoveredArticle,
    ) -> ContentFetchResult:
        try:
            extraction = await self._html_extractor.fetch(unextracted.url)
        except PermanentFetchError as e:
            logger.info(
                "content_fetch_skip",
                discovered_article_id=unextracted.id,
                reason=str(e),
            )
            return Skipped()

        match extraction:
            case ExtractionEmpty(reason=reason):
                logger.info(
                    "content_fetch_skip",
                    discovered_article_id=unextracted.id,
                    reason=reason,
                )
                return Skipped()
            case ExtractedContent() as content:
                article = article_repo.create(unextracted.id, content)
                await session.commit()
                await session.refresh(article)

                logger.info(
                    "content_fetch_completed",
                    discovered_article_id=unextracted.id,
                    article_id=article.id,
                    date_extracted=content.published_at is not None,
                )
                return Fetched(article_id=article.id)
