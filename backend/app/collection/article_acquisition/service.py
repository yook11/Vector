"""1 source 分の記事取得・変換・保存・監査をまとめる application service。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.acquisition import SourceAcquisitionAuditRepository
from app.collection.article_acquisition.errors import (
    map_origin_to_acquisition,
)
from app.collection.article_acquisition.failure_handling import (
    ArticleAcquisitionFailureHandler,
)
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
    convert_fetched_article,
    unexpected_rejection,
)
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.persistence.article_store import ArticleStore
from app.collection.sources.article_source import ArticleSource

logger = structlog.get_logger(__name__)


class ArticleAcquisitionService:
    """取得失敗は Stage 1 marker に詰め替えて Task に伝播する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: ArticleSource,
        tools_factory: Callable[[], ReaderTools] = ReaderTools,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._tools_factory = tools_factory
        self._failure_handler = ArticleAcquisitionFailureHandler(session_factory)

    async def execute(self, source_id: int) -> list[int]:
        async with self._session_factory() as session:
            article_store = ArticleStore(session)
            incomplete_repo = IncompleteArticleRepository(session)
            audit = SourceAcquisitionAuditRepository(session)
            source_name = str(self._source.name)
            persisted_ids: list[int] = []
            tools = self._tools_factory()

            try:
                async for fetched in fetch_articles(self._source, tools):
                    try:
                        outcome = convert_fetched_article(
                            fetched, source=self._source, source_id=source_id
                        )
                    except Exception as exc:
                        # entry 単位の変換 bug は source 全体を止めず rejected に畳む。
                        outcome = unexpected_rejection(
                            fetched, source=self._source, cause=exc
                        )
                    match outcome:
                        case AnalyzableArticle() as ready:
                            article_id = await article_store.save(ready)
                            if article_id is None:
                                continue
                            persisted_ids.append(article_id)
                            await audit.append_article_created(
                                source_id=source_id,
                                source_name=source_name,
                                article_id=article_id,
                                canonical_url=str(ready.source_url),
                            )
                        case ObservedArticle() as observed:
                            if await article_store.exists_by_source_url(
                                observed.source_url
                            ):
                                continue
                            incomplete_id = await incomplete_repo.save(
                                observed,
                                source_id=source_id,
                                ready_at=datetime.now(UTC),
                            )
                            if incomplete_id is None:
                                continue
                            await audit.append_incomplete_article_created(
                                source_id=source_id,
                                source_name=source_name,
                                canonical_url=str(observed.source_url),
                            )
                        case ConversionRejection() as rej:
                            await self._failure_handler.handle_conversion_rejected(
                                source_id, rej
                            )
            except (ExternalFetchError, UnreadableResponseError) as exc:
                raise map_origin_to_acquisition(exc) from exc

            await session.commit()

        logger.info(
            "acquire_source_completed",
            source_id=source_id,
            persisted_count=len(persisted_ids),
        )
        return persisted_ids
