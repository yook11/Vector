"""コンテンツ取得サービス — 記事本文と公開日時の取得・Article 行の作成を編成する。

アトミックなユースケース: ``discovered_article_id`` を受け取り、DB ルックアップ
(:class:`DiscoveredArticleLookupRepository`) の結果で早期分岐し、抽出対象であれば
HTML 抽出を :class:`ArticleHtmlExtractor` に委譲、成功時 (:class:`ExtractedContent`)
は :class:`ArticleRepository` 経由で Article 行を作成する。

結果型は「分析に進められるか」を直接表現する:

- :class:`ArticleReady`     — Article 行あり（新規抽出 / 冪等ヒット 両方）
- :class:`ExtractionFailed` — 抽出を試みたが本文を得られなかった（外部 / 品質）

DB 側の不整合（行が存在しない）はビジネス状態ではなく異常系なので、
:class:`DiscoveredArticleMissing` 例外として呼び出し側へ伝播させる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import DiscoveredArticleMissing, PermanentFetchError
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractionEmpty,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleLookupRepository,
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


ContentFetchResult = ArticleReady | ExtractionFailed


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
            ArticleReady / ExtractionFailed のいずれか。

        Raises:
            DiscoveredArticleMissing: DB に対象行が存在しない異常系。
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            lookup_repo = DiscoveredArticleLookupRepository(session)
            article_repo = ArticleRepository(session)

            lookup = await lookup_repo.find_by_id(discovered_article_id)
            if lookup is None:
                raise DiscoveredArticleMissing(discovered_article_id)

            # 冪等ヒット: 既に Article あり
            if lookup.existing_article is not None:
                logger.info(
                    "content_already_extracted",
                    discovered_article_id=lookup.id,
                    article_id=lookup.existing_article.id,
                )
                return ArticleReady(article_id=lookup.existing_article.id)

            # 抽出対象: happy path
            try:
                extraction = await self._html_extractor.fetch(lookup.original_url)
            except PermanentFetchError as e:
                logger.info(
                    "content_extraction_failed",
                    discovered_article_id=lookup.id,
                    reason="permanent_fetch_error",
                    detail=str(e),
                )
                return ExtractionFailed(reason="permanent_fetch_error")

            if isinstance(extraction, ExtractionEmpty):
                logger.info(
                    "content_extraction_failed",
                    discovered_article_id=lookup.id,
                    reason=extraction.reason,
                )
                return ExtractionFailed(reason=extraction.reason)

            # AI 境界 → Draft (sanitize / DoS 上限の defense-in-depth)
            draft = ArticleDraft.from_extracted(extraction)

            # 並行レース対応 INSERT
            persisted = await article_repo.save(draft, discovered_article_id=lookup.id)
            if persisted is None:
                # 並行レース敗北: 別ワーカーが先に書き込み済み → 読み戻して合流
                existing = await article_repo.find_by_discovered_article_id(lookup.id)
                if existing is None:
                    # ON CONFLICT 直後に行が見えないのは異常 (FK / トリガ等)
                    raise RuntimeError(
                        f"Article(discovered_article_id={lookup.id}) lost "
                        "in race recovery"
                    )
                logger.info(
                    "content_already_extracted",
                    discovered_article_id=lookup.id,
                    article_id=existing.id,
                )
                return ArticleReady(article_id=existing.id)

            await session.commit()

            article = Article.from_draft(
                draft,
                id=persisted.id,
                discovered_article_id=lookup.id,
                created_at=persisted.created_at,
            )
            logger.info(
                "content_fetch_completed",
                discovered_article_id=lookup.id,
                article_id=article.id,
                date_extracted=article.published_at is not None,
            )
            return ArticleReady(article_id=article.id)
