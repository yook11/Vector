"""Stage 2 (content_fetch) のビジネスロジック — pending_html_articles 駆動。

PR2.5-B cutover で StagedArticle (kiq envelope) 経由から
``pending_html_articles.id`` 駆動に切り替えた版。PR-E で URL 経路を
``pending.url`` (canonicalize 済み) に一本化、``articles.source_url``
を SSoT として race-loss read-back に使用する。

責務:

- ``find_by_id`` で pending を SELECT (``url`` 直接保持)
- ``status='running'`` ガードで at-least-once 重複配送を静かに弾く
- HTTP 取得 → ``ExtractionEmpty`` / ``PermanentFetchError`` の捌き
- ``TemporaryFetchError`` を per-error retry policy で次 ``ready_at`` 計算
  (max_attempts 超過なら ``mark_exhausted``)
- promotion ``Failed`` の捌き
- ``articles`` INSERT + ``pending_html_articles`` DELETE を **同 tx で一括 commit**
- race-loss (``articles.source_url UNIQUE``) を ``conflict_lost`` audit で吸収
- ``pipeline_events`` への監査書込 (success/conflict_lost/dropped_terminal/
  dropped_transient/will_retry の 5 系統)。``canonical_url`` を集計 key
  として焼き付ける。

caller (task) の責務:

- 戻り値 ``int | None`` の dispatch (chain は ``int`` (article_id) が返った
  時のみ ``extract_content.kiq``)
- ``None`` (重複配送 / 状態不整合 / 永続失敗 / 一時失敗 / race-loss) は no-op
  で exit。失敗詳細は ``pipeline_events.payload.reason_code`` で観測する。

設計上の決定:

- ``TemporaryFetchError`` は Service 内で全て catch して DB 状態更新 + audit
  に変換する (taskiq retry は使わず DB 駆動)
- ``attempt`` は ``pending.attempt_count`` を SSoT として使用 (caller から
  受け取らない、ι.2)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
)
from app.collection.extraction.repository import ArticleRepository
from app.collection.extraction.retry_policy import compute_next_delay_minutes
from app.collection.ingestion.domain.fetched_article import (
    Failed as IngestionFailed,
)
from app.collection.ingestion.domain.fetched_article import (
    ReadyForArticle,
)
from app.collection.ingestion.pending_repository import (
    PendingHtmlArticleRepository,
    PendingHtmlContext,
)
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import ContentFetchPayload
from app.observability.repository import PipelineEventRepository

logger = structlog.get_logger(__name__)


class ContentFetchService:
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
        """pending 1 件を HTML 取得 → promotion → 永続化 → 監査までの一連を担う。

        Returns:
            ``int`` — 永続化済 ``article_id``。caller は ``extract_content.kiq``
            に chain する。
            ``None`` — 重複配送 / lease 衝突 / 状態不整合 / 永続失敗 / 一時失敗 /
            race-loss (静かに exit)。失敗詳細は
            ``pipeline_events.payload.reason_code`` で観測する。
        """
        t0 = time.monotonic()
        extractor = self._extractor_factory()
        extractor_class = type(extractor).__name__

        # 入口 SELECT: pending 1 行を 1 SQL で取る
        pending = await self._load(pending_id)
        if pending is None:
            # 既に DELETE 済 (at-least-once 重複配送)
            return None
        if pending.row_meta.status != "running":
            # cron poller が claim していない (lease 衝突 / 古い message)
            return None

        # HTTP 取得 (extractor の SSRF 境界は SafeUrl を受ける)
        try:
            html_result = await extractor.fetch(
                pending.incomplete_article.source_url.as_safe_url()
            )
        except PermanentFetchError as exc:
            return await self._handle_terminal(
                pending,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code="permanent_fetch_error",
                exc=exc,
            )
        except TemporaryFetchError as exc:
            return await self._handle_temporary(
                pending,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                exc=exc,
            )

        # ExtractionEmpty (Content-Type 不一致 / parse_error / 品質ゲート未達)
        if isinstance(html_result, ExtractionEmpty):
            return await self._handle_terminal(
                pending,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code=f"extraction_empty_{html_result.reason}",
            )

        # 静的型 narrow
        assert isinstance(html_result, ExtractedContent)  # noqa: S101

        # promotion (IncompleteArticle + HTML → ReadyForArticle)
        advanced = ReadyForArticle.try_advance_from(
            pending.incomplete_article,
            body=html_result.body,
            html_published_at=html_result.published_at,
            html_title=html_result.title,
        )
        if isinstance(advanced, IngestionFailed):
            return await self._handle_terminal(
                pending,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code=f"promotion_{advanced.reason.code}",
                quality_gate_metric={
                    "body_length": len(html_result.body),
                    "failure_detail": advanced.reason.detail,
                },
            )

        # 永続化 + audit (race-loss は conflict_lost audit + None に変換)
        return await self._persist_and_audit(
            pending=pending,
            advanced=advanced,
            duration_ms=_elapsed_ms(t0),
            extractor_class=extractor_class,
            body_length=len(advanced.body),
        )

    async def _load(self, pending_id: int) -> PendingHtmlContext | None:
        """``pending_html_articles`` 1 行を SELECT。"""
        async with self._session_factory() as session:
            repo = PendingHtmlArticleRepository(session)
            return await repo.find_by_id(pending_id)

    async def _handle_temporary(
        self,
        pending: PendingHtmlContext,
        *,
        duration_ms: int,
        extractor_class: str,
        exc: TemporaryFetchError,
    ) -> None:
        """一時失敗を per-error policy で捌く。

        ``pending.attempt_count >= policy.max_attempts`` なら ``mark_exhausted``
        (status='closed') + ``dropped_transient`` (FAILED) audit。
        未満なら ``mark_will_retry(ready_at=next_at)`` (status='open' + 未来の
        ready_at) + ``will_retry`` (FAILED) audit。
        """
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        policy, delay_minutes = compute_next_delay_minutes(exc, row_meta.attempt_count)
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)
            error_class_fqn = _fqn(exc)

            if row_meta.attempt_count >= policy.max_attempts:
                await pending_repo.mark_exhausted(row_meta.id)
                reason_code = f"temporary_exhausted_{policy.code}"
                payload = ContentFetchPayload(
                    canonical_url=str(canonical_url),
                    extractor_class=extractor_class,
                    reason_code=reason_code,
                    error_message=str(exc)[:500],
                    error_chain=[error_class_fqn],
                )
                await event_repo.append(
                    stage=Stage.CONTENT_FETCH,
                    event_type=EventType.FAILED,
                    outcome_code="dropped_transient",
                    payload=payload,
                    source_id=row_meta.source_id,
                    attempt=row_meta.attempt_count,
                    duration_ms=duration_ms,
                    error_class=error_class_fqn,
                )
                await session.commit()
                return None

            next_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
            await pending_repo.mark_will_retry(row_meta.id, ready_at=next_at)
            reason_code = f"temporary_will_retry_{policy.code}"
            payload = ContentFetchPayload(
                canonical_url=str(canonical_url),
                extractor_class=extractor_class,
                reason_code=reason_code,
                error_message=str(exc)[:500],
                error_chain=[error_class_fqn],
            )
            await event_repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.FAILED,
                outcome_code="will_retry",
                payload=payload,
                source_id=row_meta.source_id,
                attempt=row_meta.attempt_count,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
            )
            await session.commit()
            return None

    async def _handle_terminal(
        self,
        pending: PendingHtmlContext,
        *,
        duration_ms: int,
        extractor_class: str,
        reason_code: str,
        exc: BaseException | None = None,
        quality_gate_metric: dict | None = None,
    ) -> None:
        """永続失敗を ``closed`` に閉じて ``dropped_terminal`` (SKIPPED) を焼く。"""
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)
            error_class_fqn = _fqn(exc) if exc is not None else None

            await pending_repo.mark_terminal(row_meta.id)
            payload = ContentFetchPayload(
                canonical_url=str(canonical_url),
                extractor_class=extractor_class,
                reason_code=reason_code,
                error_message=str(exc)[:500] if exc is not None else None,
                error_chain=[error_class_fqn] if error_class_fqn else None,
                quality_gate_metric=quality_gate_metric,
            )
            await event_repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.SKIPPED,
                outcome_code="dropped_terminal",
                payload=payload,
                source_id=row_meta.source_id,
                attempt=row_meta.attempt_count,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
            )
            await session.commit()
            return None

    async def _persist_and_audit(
        self,
        *,
        pending: PendingHtmlContext,
        advanced: ReadyForArticle,
        duration_ms: int,
        extractor_class: str,
        body_length: int,
    ) -> int | None:
        """``articles`` INSERT + ``pending_html_articles`` DELETE を同 tx で commit。

        race-loss (``save`` が ``None``) → ``find_by_source_url(canonical_url)``
        で existing を読み戻す (``articles.source_url UNIQUE`` の決勝戦)。
        検出ありなら ``conflict_lost`` audit + ``None``、検出なしは構造異常として
        ``article_persist_anomaly`` audit + ``None``。
        成功は永続化済 ``article_id`` を返す。
        """
        row_meta = pending.row_meta
        canonical_url = pending.incomplete_article.source_url
        async with self._session_factory() as session:
            article_repo = ArticleRepository(session)
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)

            draft = ArticleDraft(
                title=advanced.title,
                body=advanced.body,
                published_at=advanced.published_at,
            )
            persisted = await article_repo.save(
                draft=draft,
                source_id=advanced.source_id,
                source_url=advanced.source_url,
            )

            if persisted is None:
                # race-loss: 既存を読み戻し (canonicalize 済み URL で lookup)
                existing = await article_repo.find_by_source_url(canonical_url)
                if existing is None:
                    # 構造異常 (winner が DELETE pending → INSERT article をしたが
                    # その article がもう消えている等、通常は起きない)
                    await pending_repo.mark_terminal(row_meta.id)
                    payload = ContentFetchPayload(
                        canonical_url=str(canonical_url),
                        extractor_class=extractor_class,
                        reason_code="article_persist_anomaly",
                    )
                    await event_repo.append(
                        stage=Stage.CONTENT_FETCH,
                        event_type=EventType.SKIPPED,
                        outcome_code="dropped_terminal",
                        payload=payload,
                        source_id=row_meta.source_id,
                        attempt=row_meta.attempt_count,
                        duration_ms=duration_ms,
                    )
                    await session.commit()
                    return None

                # race-loss: pending を削除 + conflict_lost audit + None
                await pending_repo.delete_one(row_meta.id)
                payload = ContentFetchPayload(
                    canonical_url=str(canonical_url),
                    extractor_class=extractor_class,
                    reason_code="conflict_lost",
                )
                await event_repo.append(
                    stage=Stage.CONTENT_FETCH,
                    event_type=EventType.SKIPPED,
                    outcome_code="conflict_lost",
                    payload=payload,
                    source_id=row_meta.source_id,
                    article_id=existing.id,
                    attempt=row_meta.attempt_count,
                    duration_ms=duration_ms,
                )
                await session.commit()
                return None

            # 成功: pending DELETE + audit (同 tx)
            await pending_repo.delete_one(row_meta.id)
            payload = ContentFetchPayload(
                canonical_url=str(canonical_url),
                extractor_class=extractor_class,
                body_length=body_length,
            )
            await event_repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.SUCCEEDED,
                outcome_code="fetched",
                payload=payload,
                source_id=row_meta.source_id,
                article_id=persisted.id,
                attempt=row_meta.attempt_count,
                duration_ms=duration_ms,
            )
            await session.commit()
            return persisted.id


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
