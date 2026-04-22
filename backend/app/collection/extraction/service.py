"""コンテンツ取得サービス — 記事本文と公開日時の取得・Article 行の作成を編成する。

アトミックなユースケース: ``discovered_article_id`` を受け取り、DB ルックアップ
(:class:`DiscoveredArticleRepository`) の結果で早期分岐し、抽出対象であれば
HTML 抽出を :class:`ArticleHtmlExtractor` に委譲、成功時 (:class:`ExtractedContent`)
は :class:`ArticleRepository` 経由で Article 行を作成する。

結果型は「分析に進められるか」を直接表現し、進めない場合は原因の所在で分類する:

- :class:`ArticleReady`      — Article 行あり（新規抽出 / 冪等ヒット 両方）
- :class:`DiscoveredNotFound` — 事前判定で対象なし（DB 側の不整合）
- :class:`ExtractionFailed`  — 抽出を試みたが本文を得られなかった（外部 / 品質）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError
from app.collection.extraction.candidate import (
    AlreadyExtracted,
    DiscoveredNotFound,
)
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractionEmpty,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleRepository,
)

logger = structlog.get_logger(__name__)


ExtractionFailureReason = Literal[
    "permanent_fetch_error",  # 外部サイト: 403/404/410/451
    "not_html",  # 外部サイト: Content-Type 不一致
    "parse_error",  # コンテンツ品質: trafilatura パース失敗
    "quality_gate",  # コンテンツ品質: 本文短すぎ等
]


@dataclass(frozen=True)
class ArticleReady:
    """分析フェーズに進める状態 — Article 行が存在する。

    新規抽出と冪等ヒット（既に Article があった）の両方を含む。caller は
    どちらの経路で来たかを区別せず、``article_id`` を下流にチェーンするだけでよい。
    """

    article_id: int


@dataclass(frozen=True)
class ExtractionFailed:
    """抽出を試みたが本文を得られなかった — 外部 HTTP or コンテンツ品質の問題。"""

    reason: ExtractionFailureReason


ContentFetchResult = ArticleReady | DiscoveredNotFound | ExtractionFailed


class ContentFetchService:
    """1 記事の本文・公開日時取得と Article 行作成を行うアトミックなユースケース。

    責務:
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
            ArticleReady / DiscoveredNotFound / ExtractionFailed のいずれか。

        Raises:
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            lookup = await DiscoveredArticleRepository(session).lookup_for_extraction(
                discovered_article_id
            )

            # 事前判定 (1): DB 不整合 — 抽出は試みていない
            if isinstance(lookup, DiscoveredNotFound):
                logger.warning(
                    "content_fetch_discovered_not_found",
                    discovered_article_id=discovered_article_id,
                )
                return DiscoveredNotFound()

            # 事前判定 (2): 冪等ヒット — 既に Article あり
            if isinstance(lookup, AlreadyExtracted):
                logger.info(
                    "content_already_extracted",
                    discovered_article_id=discovered_article_id,
                    article_id=lookup.article_id,
                )
                return ArticleReady(article_id=lookup.article_id)

            # 抽出対象: happy path — 以降 lookup は UnextractedFound に narrow 済み
            unextracted = lookup.article

            try:
                extraction = await self._html_extractor.fetch(unextracted.url)
            except PermanentFetchError as e:
                logger.info(
                    "content_extraction_failed",
                    discovered_article_id=unextracted.id,
                    reason="permanent_fetch_error",
                    detail=str(e),
                )
                return ExtractionFailed(reason="permanent_fetch_error")

            if isinstance(extraction, ExtractionEmpty):
                logger.info(
                    "content_extraction_failed",
                    discovered_article_id=unextracted.id,
                    reason=extraction.reason,
                )
                return ExtractionFailed(reason=extraction.reason)

            # 品質ゲート通過: Article 行を作成
            article = ArticleRepository(session).create(unextracted.id, extraction)
            await session.commit()
            await session.refresh(article)

            logger.info(
                "content_fetch_completed",
                discovered_article_id=unextracted.id,
                article_id=article.id,
                date_extracted=extraction.published_at is not None,
            )
            return ArticleReady(article_id=article.id)
