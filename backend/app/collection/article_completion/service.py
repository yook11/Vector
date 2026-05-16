"""``ArticleCompletionService`` — Pattern H (IncompleteArticle → AnalyzableArticle)
への補完責務全体を担う。``pending_html_articles`` 駆動。

PR 4 で ``ContentFetchService`` から rename。「HTTP fetch する」技術名ではなく
「未完成記事を完成形に補完する」責務全体 (HTTP 取得 + 抽出 + promotion + 永続化)
を表す。案 3 cutover で「ID から処理資格を判定して load する」工程を service から
除去し、Task 層が処理開始時に ``ReadyForArticleCompletion.try_advance_from`` で
構築した厚い Ready を ``execute(ready)`` で受け取る (Stage 3/4 と同型)。
``articles.source_url UNIQUE`` が重複の構造保証で、race-loss は read-back せず
log + ``None`` で短絡する。

責務 (成功主線):

- ``ReadyForArticleCompletion`` を受け取る (precondition ``status='running'``
  は Ready 型で構造保証済、service は ID も queue 状態も知らない)
- ``_resolve_ready`` で HTML 取得 + ``ExtractionEmpty`` 判定 + promotion を
  実行し ``AnalyzableArticle`` を解決する (成功主線)
- repository で ``articles`` INSERT + ``pending_html_articles`` DELETE を
  **同 tx で一括 commit**。他 Stage (Extraction/Assessment/Embedding) と同形で
  race-loss (``save`` が ``None``) は読み戻さず log + ``None`` で短絡、
  pending は race / 成功とも DELETE、真の DB 異常は例外として伝播

失敗後処理は ``ArticleCompletionFailureHandler`` に委譲する:

- origin fetch / ``ExtractionEmpty`` / promotion / persist 異常の各失敗を
  ``CompletionDisposition`` (``Terminal`` | ``Retryable``) に**分類**して
  (``disposition.py``) handler に渡す。``pending_html_articles`` の状態遷移
  (closed / open+ready_at / exhausted) + log は handler の責務。service は
  分類して委譲するだけで、副作用は handler が完結させる (責務をファイルで分離)。

caller (task) の責務:

- 戻り値 ``int | None`` の dispatch (chain は ``int`` (article_id) が返った
  時のみ ``extract_content.kiq``)
- ``None`` (重複配送 / 状態不整合 / 永続失敗 / 一時失敗 / race-loss) は no-op
  で exit。失敗詳細は構造化ログで観測する。

設計上の決定:

- origin failure は ``ExternalFetchError`` で catch し ``disposition`` mapper で
  ``Retryable`` / ``Terminal`` に分類、retry は DB 駆動 (taskiq retry は使わない)
- retry policy は ``Retryable`` が運ぶ **データ**。handler 側で policy ごとに
  コード分岐せず ``exhausted`` 判定だけで処理経路を 1 本化する
- ``attempt`` は ``ready.attempt_count`` を SSoT として使用 (caller から
  受け取らない)
- 成功側 / 失敗側の監査焼付 (``pipeline_events``) は中途半端な構造として撤去済。
  後続で proper な audit subsystem を全 BC 横断で再導入する予定。
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.disposition import (
    classify_completion_failed,
    classify_external_fetch_error,
    classify_extraction_empty,
)
from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
)
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.completion import ArticleCompletionFailed
from app.collection.external_fetch_errors import ExternalFetchError

logger = structlog.get_logger(__name__)


class ArticleCompletionService:
    """Pattern H 2 段目 — Ready 1 件を HTML 取得 + 永続化する。

    ``execute(ready)`` が単一エントリポイント。origin failure は
    ``ExternalFetchError`` で catch し disposition に分類、retry は DB 駆動で
    caller に raise しない (taskiq retry に依存しない設計)。失敗後処理は
    ``ArticleCompletionFailureHandler`` に委譲し、service は成功主線のみを持つ。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        extractor_factory: Callable[[], ArticleHtmlExtractor] = ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._extractor_factory = extractor_factory
        self._failure_handler = ArticleCompletionFailureHandler(session_factory)

    async def execute(self, ready: ReadyForArticleCompletion) -> int | None:
        """Ready 1 件を HTML 取得 → promotion → 永続化までの一連を担う。

        precondition (``status='running'``) は ``ReadyForArticleCompletion`` で
        構造保証済 (Task 層が処理開始時に ``try_advance_from`` で構築)。service は
        ID も queue 状態も知らず、Ready を完成させるだけを責務とする。

        Returns:
            ``int`` — 永続化済 ``article_id``。caller は ``extract_content.kiq``
            に chain する。
            ``None`` — lease 衝突 / 状態不整合 / 永続失敗 / 一時失敗 /
            race-loss (静かに exit)。失敗詳細は構造化ログで観測する。
        """
        advanced = await self._resolve_ready(ready)
        if advanced is None:
            return None

        canonical_url = ready.incomplete_article.source_url
        async with self._session_factory() as session:
            result = await ArticleCompletionRepository(session).persist_completed(
                ready, advanced
            )
            await session.commit()

        if not result.pending_deleted:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                attempt_count=ready.attempt_count,
                canonical_url=str(canonical_url),
            )
            return None

        if result.article_id is None:
            logger.info(
                "article_completion_conflict_lost",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
            )
            return None

        logger.info(
            "article_completion_succeeded",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            article_id=result.article_id,
            canonical_url=str(canonical_url),
        )
        return result.article_id

    async def _resolve_ready(
        self, ready: ReadyForArticleCompletion
    ) -> AnalyzableArticle | None:
        """HTML 取得 → 抽出判定 → promotion で ``AnalyzableArticle`` を解決する。

        fetch origin failure / ``ExtractionEmpty`` / promotion 失敗はすべて
        ``CompletionDisposition`` に分類して ``ArticleCompletionFailureHandler``
        に委譲し ``None`` を返す (失敗後処理は handler の責務)。成功時のみ
        昇格済 ``AnalyzableArticle`` を返す。
        """
        extractor = self._extractor_factory()

        try:
            html_result = await extractor.fetch(
                ready.incomplete_article.source_url.as_safe_url()
            )
        except ExternalFetchError as exc:
            await self._failure_handler.handle(
                ready, classify_external_fetch_error(exc), exc=exc
            )
            return None

        if isinstance(html_result, ExtractionEmpty):
            await self._failure_handler.handle(
                ready, classify_extraction_empty(html_result)
            )
            return None

        assert isinstance(html_result, ExtractedContent)  # noqa: S101

        advanced = ready.incomplete_article.complete_with_html(
            body=html_result.body,
            html_published_at=html_result.published_at,
            html_title=html_result.title,
        )
        if isinstance(advanced, ArticleCompletionFailed):
            await self._failure_handler.handle(
                ready, classify_completion_failed(advanced)
            )
            return None

        return advanced
