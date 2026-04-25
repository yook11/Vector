"""コンテンツ取得サービス — 記事本文と公開日時の取得・Article 行の作成を編成する。

アトミックなユースケース: ``discovered_article_id`` を受け取り、DB ルックアップ
(:class:`DiscoveredArticleLookupRepository`) の結果で早期分岐し、抽出対象であれば
HTML 抽出を :class:`ArticleHtmlExtractor` に委譲、成功時 (:class:`ExtractedContent`)
は :class:`ArticleRepository` 経由で Article 行を作成する。

戻り値は ``ContentFetchOutcome`` tagged union:

- :class:`ContentFetchedOutcome`     — 新規抽出で Article 行を作成した。
- :class:`AlreadyFetchedOutcome`     — 既に Article 行が存在した（冪等ヒット
  / 並行レース敗北の合流）。
- :class:`ContentFetchSkippedOutcome` — 抽出をスキップした (``discovered_not_found``
  / ``permanent_fetch_error`` / 抽出器が返す ``ExtractionEmptyReason``)。

呼び出し側 (Task) は ``ContentFetchedOutcome`` / ``AlreadyFetchedOutcome`` の
``article.id`` を下流にチェーンし、``ContentFetchSkippedOutcome`` は dispose する。
``TemporaryFetchError`` のみ Service 境界の外（Task）でリトライ判断する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractionEmpty,
    ExtractionEmptyReason,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleLookupRepository,
)

logger = structlog.get_logger(__name__)


ContentFetchSkipReason = (
    Literal["discovered_not_found", "permanent_fetch_error"] | ExtractionEmptyReason
)


@dataclass(frozen=True, slots=True)
class ContentFetchedOutcome:
    """新規抽出で Article 行を作成した状態。"""

    article: Article


@dataclass(frozen=True, slots=True)
class AlreadyFetchedOutcome:
    """既に Article 行が存在した状態 (冪等ヒット / 並行レース合流)。"""

    article: Article


@dataclass(frozen=True, slots=True)
class ContentFetchSkippedOutcome:
    """抽出をスキップした状態。

    ``discovered_not_found``: DB に DiscoveredArticle 行が無い (enqueue 後の
    削除など、運用では稀)。``permanent_fetch_error``: 403/404/410/451 など
    リトライ不能な外部失敗。``ExtractionEmptyReason``: Content-Type 不一致 /
    パース失敗 / 品質ゲート未達 (extractor が SSoT)。

    観測性のため ``discovered_article_id`` を保持するが、URL や本文・スタック
    トレースは含めない (秘匿情報の漏出を避け、ログ側で必要な詳細を出す)。
    """

    reason: ContentFetchSkipReason
    discovered_article_id: int


ContentFetchOutcome = (
    ContentFetchedOutcome | AlreadyFetchedOutcome | ContentFetchSkippedOutcome
)


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

    async def execute(self, discovered_article_id: int) -> ContentFetchOutcome:
        """記事本文と公開日時を取得し Article 行を作成する。

        Returns:
            ``ContentFetchOutcome``: 新規抽出 / 冪等ヒット / スキップのいずれか。

        Raises:
            TemporaryFetchError: リトライ可能な失敗。判断は呼び出し側（Task）。
        """
        async with self._session_factory() as session:
            lookup_repo = DiscoveredArticleLookupRepository(session)
            article_repo = ArticleRepository(session)

            lookup = await lookup_repo.find_by_id(discovered_article_id)
            if lookup is None:
                # DB 不整合: enqueue 後の手動削除や環境取り違え等。
                # 運用では稀だが grep キーは維持して既存ダッシュボードを死なせない。
                logger.warning(
                    "fetch_content_discovered_missing",
                    discovered_article_id=discovered_article_id,
                )
                return ContentFetchSkippedOutcome(
                    reason="discovered_not_found",
                    discovered_article_id=discovered_article_id,
                )

            # 冪等ヒット: 既に Article あり
            if lookup.existing_article is not None:
                logger.info(
                    "content_already_extracted",
                    discovered_article_id=lookup.id,
                    article_id=lookup.existing_article.id,
                )
                return AlreadyFetchedOutcome(article=lookup.existing_article)

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
                return ContentFetchSkippedOutcome(
                    reason="permanent_fetch_error",
                    discovered_article_id=lookup.id,
                )

            if isinstance(extraction, ExtractionEmpty):
                logger.info(
                    "content_extraction_failed",
                    discovered_article_id=lookup.id,
                    reason=extraction.reason,
                )
                return ContentFetchSkippedOutcome(
                    reason=extraction.reason,
                    discovered_article_id=lookup.id,
                )

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
                return AlreadyFetchedOutcome(article=existing)

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
                has_published_at=article.published_at is not None,
            )
            return ContentFetchedOutcome(article=article)
