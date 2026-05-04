"""新ルート Ingestion Service — Pattern R は 1 段、Pattern H は 2 段で取り込む。

Fetcher の ``AsyncIterator[FetchOutcome]`` を回し ``match`` で分岐する:

- ``FetchedEntry(item=ReadyForArticle)`` → discovered + articles を 1 tx で永続化
- ``FetchedEntry(item=PendingHtmlFetch)`` → discovered のみ作って ``StagedArticle``
  を ``extract_html_body.kiq`` に橋渡し (Article 作成は 2 段目 task)
- ``Failed`` → 構造化ログ + ``failed_codes`` 集計

``commit`` までが Service の責務。下流 task (``extract_content.kiq`` /
``extract_html_body.kiq``) の投入は呼び出し側が行う。``NewsSource`` ORM の
lookup は ``IngestSourceArg`` 経由で Task が済ませている前提で本 Service では行わない。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.repository import ArticleRepository
from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
)
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedEntry,
    PendingHtmlFetch,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.collection.ingestion.staged import StagedArticle
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import SourceFetchPayload
from app.observability.repository import PipelineEventRepository
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestedOutcome:
    """Outcome 純化原則: 「次の段階に渡す価値があるもの」のみ持つ。

    観測値 (failed/skipped count, metadata 観測) は同 tx で ``pipeline_events``
    に焼き付け済 (memory ``feedback_outcome_purification``)。
    """

    persisted: list[Article]
    staged: list[StagedArticle]


class IngestionService:
    """ソース 1 件を新 Protocol Fetcher 経由で 1 段取り込みするユースケース。

    ``PermanentFetchError`` / ``TemporaryFetchError`` は呼び出し側 (Task) に
    伝播する (retry 判断は Task 層の責務)。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher_factory: Callable[[], Fetcher],
    ) -> None:
        self._session_factory = session_factory
        self._fetcher_factory = fetcher_factory

    async def execute(self, source_id: int, *, attempt: int = 1) -> IngestedOutcome:
        t0 = time.monotonic()
        async with self._session_factory() as session:
            fetcher = self._fetcher_factory()

            persisted: list[Article] = []
            staged: list[StagedArticle] = []
            failed_codes: Counter[str] = Counter()
            skipped_count = 0
            ready_count = 0
            pending_count = 0
            metadata_fields_observed: set[str] = set()
            metadata_sample: dict[str, Any] | None = None

            try:
                async for outcome in fetcher.fetch(source_id):
                    match outcome:
                        case FetchedEntry(item=ReadyForArticle() as ready, metadata=md):
                            ready_count += 1
                            metadata_sample = self._observe_metadata(
                                md, metadata_fields_observed, metadata_sample
                            )
                            article = await self._persist_one(session, source_id, ready)
                            if article is not None:
                                persisted.append(article)
                            else:
                                skipped_count += 1
                        case FetchedEntry(
                            item=PendingHtmlFetch() as pending, metadata=md
                        ):
                            pending_count += 1
                            metadata_sample = self._observe_metadata(
                                md, metadata_fields_observed, metadata_sample
                            )
                            discovered_id = await self._upsert_discovered_url(
                                session,
                                source_id,
                                pending.source_url,
                                pending.title,
                            )
                            if discovered_id is None:
                                skipped_count += 1
                                continue
                            staged.append(
                                StagedArticle(
                                    discovered_id=discovered_id, pending=pending
                                )
                            )
                        case Failed(reason=r):
                            failed_codes[r.code] += 1
                            logger.warning(
                                "ingest_source_entry_failed",
                                source_id=source_id,
                                code=r.code,
                                retryable=r.retryable,
                                detail=r.detail,
                            )
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e

            failed_count = sum(failed_codes.values())
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._record_success_event(
                session=session,
                source_id=source_id,
                fetcher=fetcher,
                persisted_count=len(persisted),
                staged_count=len(staged),
                failed_count=failed_count or None,
                skipped_count=skipped_count or None,
                failed_codes=dict(failed_codes) or None,
                metadata_fields_observed=sorted(metadata_fields_observed) or None,
                metadata_sample=metadata_sample,
                attempt=attempt,
                duration_ms=duration_ms,
            )
            await session.commit()

        logger.info(
            "ingest_source_completed",
            source_id=source_id,
            ready_count=ready_count,
            pending_count=pending_count,
            failed_count=failed_count,
            persisted_count=len(persisted),
            staged_count=len(staged),
            skipped_count=skipped_count,
        )
        return IngestedOutcome(persisted=persisted, staged=staged)

    @staticmethod
    def _observe_metadata(
        md: Mapping[str, Any],
        observed: set[str],
        sample: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """metadata の key 集合を累積し、最初の non-empty entry を sample に保持。

        None / 空文字 / 空コンテナはキーが「提供された」と見なさず除外。
        """
        non_empty: dict[str, Any] = {}
        for name, val in md.items():
            if val in (None, "", (), [], {}):
                continue
            observed.add(name)
            non_empty[name] = val
        if sample is None and non_empty:
            return non_empty
        return sample

    async def _record_success_event(
        self,
        *,
        session: AsyncSession,
        source_id: int,
        fetcher: Fetcher,
        persisted_count: int,
        staged_count: int,
        failed_count: int | None,
        skipped_count: int | None,
        failed_codes: dict[str, int] | None,
        metadata_fields_observed: list[str] | None,
        metadata_sample: dict[str, Any] | None,
        attempt: int,
        duration_ms: int,
    ) -> None:
        """Stage 1 成功イベントを同 tx で ``pipeline_events`` に焼き付ける。"""
        repo = PipelineEventRepository(session)
        payload = SourceFetchPayload(
            fetcher_class=type(fetcher).__name__,
            persisted_count=persisted_count,
            staged_count=staged_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            failed_codes=failed_codes,
            metadata_fields_observed=metadata_fields_observed,
            metadata_sample=metadata_sample,
        )
        await repo.append(
            stage=Stage.SOURCE_FETCH,
            event_type=EventType.SUCCEEDED,
            outcome_code="fetched",  # ADR §既決事項: 成功は 1 本 (件数で分けない)
            payload=payload,
            source_id=source_id,
            attempt=attempt,
            duration_ms=duration_ms,
        )

    async def _persist_one(
        self,
        session: AsyncSession,
        source_id: int,
        ready: ReadyForArticle,
    ) -> Article | None:
        """1 entry を discovered + articles に永続化して Entity を返す。

        Race recovery: discovered の ``save_many`` 空 → ``find_by_url``、
        article の ``save`` None → ``find_by_discovered_article_id``。
        どちらの読み戻しも失敗で ``None`` (= skipped カウント)。
        """
        discovered_id = await self._upsert_discovered_url(
            session, source_id, ready.source_url, ready.title
        )
        if discovered_id is None:
            return None

        article_repo = ArticleRepository(session)
        draft = ArticleDraft(
            title=ready.title,
            body=ready.body,
            published_at=ready.published_at,
        )
        persisted = await article_repo.save(
            draft=draft,
            discovered_article_id=discovered_id,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
        if persisted is not None:
            return Article.from_draft(
                draft,
                id=persisted.id,
                discovered_article_id=discovered_id,
                created_at=persisted.created_at,
            )

        return await article_repo.find_by_discovered_article_id(discovered_id)

    async def _upsert_discovered_url(
        self,
        session: AsyncSession,
        source_id: int,
        source_url: SafeUrl,
        title: str,
    ) -> int | None:
        candidate = ArticleCandidate(url=source_url, title=title)
        draft = DiscoveredArticleDraft.from_candidate(
            candidate, news_source_id=source_id
        )
        repo = DiscoveredArticleRepository(session)
        results = await repo.save_many([draft])
        if results:
            return results[0].id
        existing = await repo.find_by_url(source_url)
        return existing.id if existing else None
