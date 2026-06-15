"""未完成記事を scrape / complete / persist の順で完成形に補完する use case。"""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_completion.completer import ArticleHtmlCompleter
from app.collection.article_completion.completion_failure import (
    CompletionRejection,
)
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import (
    ArticleCompletionRepository,
    CompletionSucceeded,
    CompletionSuperseded,
    CompletionUrlConflict,
)
from app.collection.article_completion.scraper import (
    ArticleScraper,
    ScrapedContent,
)
from app.collection.domain.analyzable_article import AnalyzableArticle

logger = structlog.get_logger(__name__)


class ArticleCompletionService:
    """Ready 1 件の scrape / complete / persist を orchestration する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scraper_factory: Callable[[], ArticleScraper] = ArticleScraper,
    ) -> None:
        self._session_factory = session_factory
        self._scraper_factory = scraper_factory
        self._completer = ArticleHtmlCompleter()
        self._failure_handler = ArticleCompletionFailureHandler(session_factory)

    async def execute(self, ready: ReadyForArticleCompletion) -> int | None:
        """Ready 1 件を補完し、成功時は ``analyzable_article_id``、失敗時は ``None``
        を返す。

        scrape / complete の失敗は handler が状態遷移と audit を完了させる。
        persist の DB 例外は別 tx で audit したうえで再 raise する。
        """
        # scrape: URL → 抽出物または失敗値。scrape は never raise の二値。
        scraper = self._scraper_factory()
        scraped = await scraper.scrape(ready.source_url.as_safe_url())
        match scraped:
            case ScrapedContent() as content:
                pass
            case failure:  # ScrapeFailure (transport + content の 5 variant)
                await self._failure_handler.handle_scrape_failure(ready, failure)
                return None

        # complete: 抽出物 → AnalyzableArticle またはドメイン拒絶。
        completed = self._completer.complete(ready, content)
        match completed:
            case AnalyzableArticle() as article:
                pass
            case CompletionRejection() as rejection:
                await self._failure_handler.handle_completion_rejected(ready, rejection)
                return None
            case unreachable:
                assert_never(unreachable)

        # persist: 成功 / race-loss は同一 tx、DB 例外は別 tx で audit。
        try:
            async with self._session_factory() as session:
                outcome = await ArticleCompletionRepository(session).persist_completed(
                    ready, article
                )
                await ArticleCompletionAuditRepository(session).append_persist_outcome(
                    ready=ready, outcome=outcome, advanced=article
                )
                await session.commit()
        except Exception as exc:
            await self._failure_handler.handle_persist_crashed(ready, exc)
            raise

        match outcome:
            case CompletionSuperseded():
                logger.info(
                    "article_completion_stale_attempt_ignored",
                    incomplete_article_id=ready.incomplete_article_id,
                    source_id=ready.source_id,
                    attempt_count=ready.attempt_count,
                    canonical_url=str(ready.source_url),
                )
                return None
            case CompletionUrlConflict():
                logger.info(
                    "article_completion_conflict_lost",
                    incomplete_article_id=ready.incomplete_article_id,
                    source_id=ready.source_id,
                    canonical_url=str(ready.source_url),
                )
                return None
            case CompletionSucceeded(analyzable_article_id=analyzable_article_id):
                logger.info(
                    "article_completion_succeeded",
                    incomplete_article_id=ready.incomplete_article_id,
                    source_id=ready.source_id,
                    analyzable_article_id=analyzable_article_id,
                    canonical_url=str(ready.source_url),
                )
                return analyzable_article_id
            case unreachable:
                assert_never(unreachable)
