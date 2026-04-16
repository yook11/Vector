"""Content fetch service — orchestrates article body retrieval and persistence.

Atomic use case: given an ``article_id``, load the record, delegate body
fetching to :class:`ArticleBodyFetcher`, and persist the result (or mark the
article permanently skipped). Session management is internal.
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
    """Result of content fetch use case."""

    status: Literal["fetched", "already_exists", "skipped"]


class ContentFetchService:
    """Atomic use case: fetch article body for a single article and persist.

    Responsibilities are split clearly:
      1. Load the article record (DB).
      2. Fetch the body text (delegated to ``ArticleBodyFetcher``).
      3. Persist the body — or mark the article permanently skipped.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        body_fetcher: ArticleBodyFetcher,
    ) -> None:
        self._session_factory = session_factory
        self._body_fetcher = body_fetcher

    async def execute(self, article_id: int) -> ContentFetchResult:
        """Fetch and persist the article body.

        Returns:
            ContentFetchResult with status.

        Raises:
            TemporaryFetchError: retryable failure — caller (Task) decides.
        """
        async with self._session_factory() as session:
            # 1. Load the article record
            article = await session.get(NewsArticle, article_id)
            if article is None:
                logger.warning("content_fetch_article_not_found", article_id=article_id)
                return ContentFetchResult("skipped")
            if article.original_content is not None:
                return ContentFetchResult("already_exists")

            # 2. Delegate body fetch
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

            # Quality gate rejection — permanent
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

            # 3. Persist
            article.original_content = content
            session.add(article)
            await session.commit()
            logger.info("content_fetch_completed", article_id=article_id)
            return ContentFetchResult("fetched")


async def mark_article_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    article_id: int,
) -> None:
    """Mark an article for permanent skip (for Task last-attempt use)."""
    async with session_factory() as session:
        article = await session.get(NewsArticle, article_id)
        if article is not None:
            article.skip_content_fetch = True
            session.add(article)
            await session.commit()
