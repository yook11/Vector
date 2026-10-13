"""1 source 分の記事取得・変換・保存・監査をまとめる application service。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.audit.stages.acquisition import SourceAcquisitionAuditRepository
from app.collection.article_acquisition.events import (
    IncompleteArticleRecorded,
)
from app.collection.article_acquisition.failure_recording import (
    ArticleAcquisitionFailureRecorder,
)
from app.collection.article_acquisition.fetched_article import (
    FetchedArticle,
    FetchedArticleTitleMissingError,
)
from app.collection.article_acquisition.fetched_article_converter import (
    convert_fetched_article,
)
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.metrics import (
    AcquisitionEntryOutcome,
    record_acquisition_outcome,
)
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_url import ArticleUrlInvalidError
from app.collection.domain.observed_article import ObservedArticle
from app.collection.events import AnalyzableArticleCreated
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.collection.sources.article_source import AcquirableSource
from app.db.session import SessionFactory
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.models.outbox_event import OutboxEvent


class ArticleAcquisitionService:
    """取得失敗は発生した例外のまま呼び出し側へ伝える。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        source: AcquirableSource,
        tools_factory: Callable[[], ReaderTools] = ReaderTools,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._tools_factory = tools_factory
        self._failure_recorder = ArticleAcquisitionFailureRecorder(session_factory)

    async def execute(self, source_id: int) -> list[int]:
        async with self._session_factory() as session:
            article_repo = AnalyzableArticleRepository(session)
            incomplete_repo = IncompleteArticleRepository(session)
            audit = SourceAcquisitionAuditRepository(session)
            source_name = str(self._source.name)
            persisted_ids: list[int] = []
            observed_count = 0
            tools = self._tools_factory()

            async for raw_article in fetch_articles(self._source, tools):
                try:
                    outcome = convert_fetched_article(
                        FetchedArticle.from_raw(raw_article),
                        source=self._source,
                        source_id=source_id,
                    )
                except (ArticleUrlInvalidError, FetchedArticleTitleMissingError) as exc:
                    create_policy_json_logger(
                        __name__, EXTERNAL_CONTENT_LOG_RULES
                    ).warning(
                        "article_conversion_rejected",
                        source_name=source_name,
                        exc_info=exc,
                    )
                    # 棄却の件数は別トランザクションの監査commit後に計上する。
                    await self._failure_recorder.record_conversion_rejected(
                        source_id=source_id,
                        source_name=source_name,
                        raw_article=raw_article,
                        error=exc,
                    )
                    continue
                except Exception as exc:
                    # entry 単位の変換 bug は source 全体を止めず rejected に畳む。
                    create_policy_json_logger(
                        __name__, EXTERNAL_CONTENT_LOG_RULES
                    ).error(
                        "fetched_article_conversion_unexpected",
                        source_name=source_name,
                        exc_info=exc,
                    )
                    await self._failure_recorder.record_conversion_rejected(
                        source_id=source_id,
                        source_name=source_name,
                        raw_article=raw_article,
                        error=exc,
                    )
                    continue
                match outcome:
                    case AnalyzableArticle() as ready:
                        analyzable_article_id = await article_repo.save(ready)
                        if analyzable_article_id is None:
                            continue
                        persisted_ids.append(analyzable_article_id)
                        await audit.append_article_created(
                            source_id=source_id,
                            source_name=source_name,
                            analyzable_article_id=analyzable_article_id,
                            canonical_url=str(ready.source_url),
                        )
                        event = AnalyzableArticleCreated(
                            analyzable_article_id=analyzable_article_id,
                        )
                        session.add(
                            OutboxEvent(
                                event_type=AnalyzableArticleCreated.EVENT_TYPE,
                                schema_version=AnalyzableArticleCreated.SCHEMA_VERSION,
                                payload=event.model_dump(mode="json"),
                            )
                        )
                    case ObservedArticle() as observed:
                        if await article_repo.exists_by_source_url(observed.source_url):
                            continue
                        incomplete_id = await incomplete_repo.save(
                            observed,
                            source_id=source_id,
                            ready_at=datetime.now(UTC),
                        )
                        if incomplete_id is None:
                            continue
                        observed_count += 1
                        await audit.append_incomplete_article_created(
                            source_id=source_id,
                            source_name=source_name,
                            canonical_url=str(observed.source_url),
                        )
                        event = IncompleteArticleRecorded(
                            source_id=source_id,
                            incomplete_article_id=incomplete_id,
                        )
                        session.add(
                            OutboxEvent(
                                event_type=IncompleteArticleRecorded.EVENT_TYPE,
                                schema_version=(
                                    IncompleteArticleRecorded.SCHEMA_VERSION
                                ),
                                payload=event.model_dump(mode="json"),
                            )
                        )

            await session.commit()

        # analyzable/observed の監査は本 session と同一 tx。commit 成功後にだけ計上し
        # 監査行と件数を揃える (dedup skip は continue で非計数、run 失敗時は rollback
        # され未 emit)。rejected は別 tx 監査のため Recorder が計上する。
        record_acquisition_outcome(
            AcquisitionEntryOutcome.ANALYZABLE, count=len(persisted_ids)
        )
        record_acquisition_outcome(
            AcquisitionEntryOutcome.OBSERVED, count=observed_count
        )
        return persisted_ids
