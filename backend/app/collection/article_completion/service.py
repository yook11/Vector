"""``ArticleCompletionService`` — Pattern H (IncompleteArticle → ReadyForArticle)
への補完責務全体を担う。``pending_html_articles`` 駆動。

PR 4 で ``ContentFetchService`` から rename。「HTTP fetch する」技術名ではなく
「未完成記事を完成形に補完する」責務全体 (HTTP 取得 + 抽出 + promotion + 永続化)
を表す。PR2.5-B cutover で StagedArticle (kiq envelope) 経由から
``pending_html_articles.id`` 駆動に切り替えた版。PR-E で URL 経路を
``pending.url`` (canonicalize 済み) に一本化、``articles.source_url`` を SSoT
として race-loss read-back に使用する。

責務:

- ``find_by_id`` で pending を SELECT (``url`` 直接保持)
- ``status='running'`` ガードで at-least-once 重複配送を静かに弾く
- HTTP 取得 → ``ExtractionEmpty`` / ``PermanentFetchError`` の捌き
- ``TemporaryFetchError`` を per-error retry policy で次 ``ready_at`` 計算
  (max_attempts 超過なら ``mark_exhausted``)
- promotion ``ArticleCompletionFailed`` の捌き
- ``articles`` INSERT + ``pending_html_articles`` DELETE を **同 tx で一括 commit**
- race-loss (``articles.source_url UNIQUE``) を ``find_by_source_url`` 読み戻しで
  吸収 (pending を DELETE、敗者側の article は INSERT しない)

caller (task) の責務:

- 戻り値 ``int | None`` の dispatch (chain は ``int`` (article_id) が返った
  時のみ ``extract_content.kiq``)
- ``None`` (重複配送 / 状態不整合 / 永続失敗 / 一時失敗 / race-loss) は no-op
  で exit。失敗詳細は構造化ログで観測する。

設計上の決定:

- ``TemporaryFetchError`` は Service 内で全て catch して DB 状態更新に変換する
  (taskiq retry は使わず DB 駆動)
- ``attempt`` は ``pending.attempt_count`` を SSoT として使用 (caller から
  受け取らない、ι.2)
- 成功側 / 失敗側の監査焼付 (``pipeline_events``) は中途半端な構造として撤去済。
  後続で proper な audit subsystem を全 BC 横断で再導入する予定。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.repository import ArticleRepository
from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
)
from app.collection.article_completion.retry_policy import compute_next_delay_minutes
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.incomplete_article.domain.completion import ArticleCompletionFailed
from app.collection.incomplete_article.repository import (
    PendingHtmlArticleRepository,
    PendingHtmlContext,
)

logger = structlog.get_logger(__name__)


class ArticleCompletionService:
    """Pattern H 2 段目 — pending 1 件を HTML 取得 + 永続化する。

    ``execute(pending_id)`` が単一エントリポイント。``TemporaryFetchError``
    は内部で catch して per-error policy で DB 状態を更新するため、caller
    に raise しない (taskiq retry に依存しない設計)。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        extractor_factory: Callable[[], ArticleHtmlExtractor] = ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._extractor_factory = extractor_factory

    async def execute(self, pending_id: int) -> int | None:
        """pending 1 件を HTML 取得 → promotion → 永続化までの一連を担う。

        Returns:
            ``int`` — 永続化済 ``article_id``。caller は ``extract_content.kiq``
            に chain する。
            ``None`` — 重複配送 / lease 衝突 / 状態不整合 / 永続失敗 / 一時失敗 /
            race-loss (静かに exit)。失敗詳細は構造化ログで観測する。
        """
        extractor = self._extractor_factory()

        pending = await self._load(pending_id)
        if pending is None:
            return None
        if pending.row_meta.status != "running":
            return None

        try:
            html_result = await extractor.fetch(
                pending.incomplete_article.source_url.as_safe_url()
            )
        except PermanentFetchError as exc:
            return await self._handle_terminal(
                pending, reason="permanent_fetch_error", exc=exc
            )
        except TemporaryFetchError as exc:
            return await self._handle_temporary(pending, exc=exc)

        if isinstance(html_result, ExtractionEmpty):
            return await self._handle_terminal(
                pending, reason=f"extraction_empty_{html_result.reason}"
            )

        assert isinstance(html_result, ExtractedContent)  # noqa: S101

        advanced = pending.incomplete_article.complete_with_html(
            body=html_result.body,
            html_published_at=html_result.published_at,
            html_title=html_result.title,
        )
        if isinstance(advanced, ArticleCompletionFailed):
            return await self._handle_terminal(
                pending,
                reason=f"promotion_{advanced.reason.code}",
                detail=advanced.reason.detail,
            )

        return await self._persist(pending, advanced)

    async def _load(self, pending_id: int) -> PendingHtmlContext | None:
        """``pending_html_articles`` 1 行を SELECT。"""
        async with self._session_factory() as session:
            repo = PendingHtmlArticleRepository(session)
            return await repo.find_by_id(pending_id)

    async def _handle_temporary(
        self,
        pending: PendingHtmlContext,
        *,
        exc: TemporaryFetchError,
    ) -> None:
        """一時失敗を per-error policy で捌く。

        ``pending.attempt_count >= policy.max_attempts`` なら ``mark_exhausted``
        (status='closed')、未満なら ``mark_will_retry(ready_at=next_at)``
        (status='open' + 未来の ready_at)。
        """
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        policy, delay_minutes = compute_next_delay_minutes(exc, row_meta.attempt_count)
        exhausted = row_meta.attempt_count >= policy.max_attempts
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            if exhausted:
                await pending_repo.mark_exhausted(row_meta.id)
            else:
                next_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
                await pending_repo.mark_will_retry(row_meta.id, ready_at=next_at)
            await session.commit()

        logger.warning(
            "article_completion_temporary",
            pending_id=row_meta.id,
            source_id=row_meta.source_id,
            canonical_url=str(canonical_url),
            policy_code=policy.code,
            exhausted=exhausted,
            attempt_count=row_meta.attempt_count,
            error_class=type(exc).__name__,
        )
        return None

    async def _handle_terminal(
        self,
        pending: PendingHtmlContext,
        *,
        reason: str,
        exc: BaseException | None = None,
        detail: str | None = None,
    ) -> None:
        """永続失敗を ``closed`` に閉じる。"""
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            await pending_repo.mark_terminal(row_meta.id)
            await session.commit()

        logger.warning(
            "article_completion_terminal",
            pending_id=row_meta.id,
            source_id=row_meta.source_id,
            canonical_url=str(canonical_url),
            reason=reason,
            error_class=type(exc).__name__ if exc is not None else None,
            detail=detail,
        )
        return None

    async def _persist(
        self,
        pending: PendingHtmlContext,
        advanced: ReadyForArticle,
    ) -> int | None:
        """``articles`` INSERT + ``pending_html_articles`` DELETE を同 tx で commit。

        race-loss (``save_ready`` が ``None``) → ``find_by_source_url(canonical_url)``
        で existing を読み戻す (``articles.source_url UNIQUE`` の決勝戦)。
        検出ありなら pending DELETE + ``None``、検出なしは構造異常として
        pending を ``closed`` に閉じて ``None``。
        成功は永続化済 ``article_id`` を返す。
        """
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        async with self._session_factory() as session:
            article_repo = ArticleRepository(session)
            pending_repo = PendingHtmlArticleRepository(session)

            article_id = await article_repo.save_ready(advanced)
            if article_id is None:
                existing = await article_repo.find_by_source_url(canonical_url)
                if existing is None:
                    await pending_repo.mark_terminal(row_meta.id)
                    await session.commit()
                    logger.warning(
                        "article_completion_persist_anomaly",
                        pending_id=row_meta.id,
                        source_id=row_meta.source_id,
                        canonical_url=str(canonical_url),
                    )
                    return None

                await pending_repo.delete_one(row_meta.id)
                await session.commit()
                logger.info(
                    "article_completion_conflict_lost",
                    pending_id=row_meta.id,
                    source_id=row_meta.source_id,
                    article_id=existing.id,
                    canonical_url=str(canonical_url),
                )
                return None

            await pending_repo.delete_one(row_meta.id)
            await session.commit()

        logger.info(
            "article_completion_succeeded",
            pending_id=row_meta.id,
            source_id=row_meta.source_id,
            article_id=article_id,
            canonical_url=str(canonical_url),
        )
        return article_id
