"""新ルート Ingestion Service — Pattern R / Pattern H 振り分けの 1 段ユースケース。

新 2 表構成 (``articles`` / ``pending_html_articles``) を直接駆動する。

Fetcher の ``AsyncIterator[FetchOutcome]`` を回し ``match`` で分岐する:

- ``FetchedEntry(item=ReadyForArticle)`` → ``articles.source_url`` を主軸に
  直 INSERT (Pattern R)。``source_url`` は ``CanonicalArticleUrl`` 型で
  canonicalize 済が構造保証されているため Service 側で後付け正規化は不要。
  ``ON CONFLICT DO NOTHING`` で同 tick race / 既知 URL を吸収
  (``None`` → ``known_url`` skip)。caller (``ingest_source`` task) が
  ``extract_content.kiq`` に chain する。
- ``FetchedEntry(item=PendingHtmlFetch)`` → ``article_repo.exists_by_source_url``
  pre-check で feed 再露出を弾き、``pending_html_articles.url`` で投入
  (Pattern H)。``url`` は ``CanonicalArticleUrl`` 型で canonical 保証済。
  下流は cron poller (``dispatch_html_fetch_jobs``) が DB 駆動で拾うため、
  Service / Task は pending_id を caller に渡さない (``IngestedOutcome`` 純化)。
- ``Failed`` → 構造化ログ + ``failed_codes`` 集計、永続化に流れない。

``commit`` までが Service の責務。``NewsSource`` ORM の lookup は
``IngestSourceArg`` (=task envelope) で済んでいるため本 Service では行わない。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.repository import ArticleRepository
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedEntry,
    PendingHtmlFetch,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.pending_repository import PendingHtmlArticleRepository
from app.collection.ingestion.staged_attributes import StagedArticleAttributes
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import SourceFetchPayload
from app.observability.repository import PipelineEventRepository
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestedOutcome:
    """Outcome 純化原則: 「次の段階に渡す価値があるもの」のみ持つ。

    Pattern R で永続化された ``Article`` のみを caller (``ingest_source``
    task) に返す。caller は ``ReadyForExtraction`` を構築して
    ``extract_content.kiq`` に流す。

    Pattern H 経路で投入された ``pending_html_articles`` 行は cron poller
    (``dispatch_html_fetch_jobs``) が DB 駆動で拾うため、Outcome として
    持ち回らない。観測値 (failed/skipped/completion_queued count, metadata
    観測) は同 tx で ``pipeline_events`` に焼き付け済
    (memory ``feedback_outcome_purification``)。
    """

    persisted: list[Article]


class IngestionService:
    """ソース 1 件を新 Protocol Fetcher 経由で取り込み、新 3 表に振り分ける。

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
            article_repo = ArticleRepository(session)
            pending_repo = PendingHtmlArticleRepository(session)

            persisted: list[Article] = []
            article_created = 0
            completion_queued = 0
            skipped_codes: Counter[str] = Counter()
            failed_codes: Counter[str] = Counter()
            metadata_fields_observed: set[str] = set()
            metadata_sample: dict[str, Any] | None = None

            try:
                async for outcome in fetcher.fetch(source_id):
                    match outcome:
                        case FetchedEntry(item=ReadyForArticle() as ready, metadata=md):
                            metadata_sample = self._observe_metadata(
                                md, metadata_fields_observed, metadata_sample
                            )
                            article = await self._persist_ready(
                                article_repo, ready=ready
                            )
                            if article is None:
                                # ``articles.source_url UNIQUE`` ON CONFLICT 衝突
                                # = 既知 URL (再露出 / 同 tick の他経路勝者)
                                skipped_codes["known_url"] += 1
                                continue
                            article_created += 1
                            persisted.append(article)
                        case FetchedEntry(
                            item=PendingHtmlFetch() as pending, metadata=md
                        ):
                            metadata_sample = self._observe_metadata(
                                md, metadata_fields_observed, metadata_sample
                            )
                            # pre-check: feed 再露出時に既知 URL の HTML fetch を
                            # 反復しないための実用的 idempotency (ロックではない)
                            if await article_repo.exists_by_source_url(
                                pending.source_url
                            ):
                                skipped_codes["known_url"] += 1
                                continue
                            pending_id = await pending_repo.create(
                                url=pending.source_url,
                                source_id=source_id,
                                staged_attributes=StagedArticleAttributes(
                                    title=pending.title,
                                    published_at_hint=pending.published_at_hint,
                                    prefer_html_title=pending.prefer_html_title,
                                ),
                                ready_at=datetime.now(UTC),
                            )
                            if pending_id is None:
                                # ``pending_html_articles.url UNIQUE`` 衝突
                                # = 別 worker が同 URL を既に pending 化済
                                skipped_codes["existing_pending"] += 1
                                continue
                            completion_queued += 1
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

            skipped_count = sum(skipped_codes.values())
            failed_count = sum(failed_codes.values())
            entry_count = (
                article_created + completion_queued + skipped_count + failed_count
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._record_success_event(
                session=session,
                source_id=source_id,
                fetcher=fetcher,
                entry_count=entry_count,
                article_created_count=article_created,
                completion_queued_count=completion_queued,
                skipped_count=skipped_count,
                failed_count=failed_count,
                completion_reason_codes=(
                    {"html_required": completion_queued} if completion_queued else None
                ),
                skipped_codes=dict(skipped_codes) or None,
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
            entry_count=entry_count,
            article_created_count=article_created,
            completion_queued_count=completion_queued,
            skipped_count=skipped_count,
            failed_count=failed_count,
        )
        return IngestedOutcome(persisted=persisted)

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
        entry_count: int,
        article_created_count: int,
        completion_queued_count: int,
        skipped_count: int,
        failed_count: int,
        completion_reason_codes: dict[str, int] | None,
        skipped_codes: dict[str, int] | None,
        failed_codes: dict[str, int] | None,
        metadata_fields_observed: list[str] | None,
        metadata_sample: dict[str, Any] | None,
        attempt: int,
        duration_ms: int,
    ) -> None:
        """Stage 1 成功イベントを同 tx で ``pipeline_events`` に焼き付ける。

        κ: 5 種 count を常時 populate (entry_count == sum(...) invariant)。
        """
        repo = PipelineEventRepository(session)
        payload = SourceFetchPayload(
            fetcher_class=type(fetcher).__name__,
            entry_count=entry_count,
            article_created_count=article_created_count,
            completion_queued_count=completion_queued_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            completion_reason_codes=completion_reason_codes,
            skipped_codes=skipped_codes,
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

    async def _persist_ready(
        self,
        article_repo: ArticleRepository,
        *,
        ready: ReadyForArticle,
    ) -> Article | None:
        """Pattern R 1 entry を ``articles`` に直 INSERT して Entity を返す。

        ``source_url`` は ``CanonicalArticleUrl`` 型で canonicalize 済が構造保証
        されているため、Service 側で後付け正規化は行わない
        (``articles.source_url UNIQUE`` は canonical 値で効く)。

        Race recovery: ``save`` が ``None`` を返した場合は他 worker / 別 yield
        が同 URL で先に書き込み済。既に articles に存在するため Pattern R の
        文脈では skip 扱い、Entity を返す必要はない。
        """
        draft = ArticleDraft(
            title=ready.title,
            body=ready.body,
            published_at=ready.published_at,
        )
        persisted = await article_repo.save(
            draft=draft,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
        if persisted is None:
            return None
        return Article.from_draft(
            draft, id=persisted.id, created_at=persisted.created_at
        )
