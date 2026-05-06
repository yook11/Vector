"""Stage 2 (content_fetch) のビジネスロジック — pending_html_articles 駆動。

PR2.5-B cutover で StagedArticle (kiq envelope) 経由から
``pending_html_articles.id`` 駆動に切り替えた版。

責務:

- ``find_by_id`` で pending を SELECT (article_urls JOIN で normalized_url 同梱)
- ``status='running'`` ガードで at-least-once 重複配送を静かに弾く
- HTTP 取得 → ``ExtractionEmpty`` / ``PermanentFetchError`` の捌き
- ``TemporaryFetchError`` を per-error retry policy で次 ``ready_at`` 計算
  (max_attempts 超過なら ``mark_exhausted``)
- promotion ``Failed`` の捌き
- ``articles`` INSERT + ``pending_html_articles`` DELETE を **同 tx で一括 commit**
- race-loss を ``ConflictLost`` (audit) で吸収
- ``pipeline_events`` への監査書込 (success/conflict_lost/dropped_terminal/
  dropped_transient/will_retry の 5 系統)

caller (task) の責務:

- 戻り値 ``ContentFetchOutcome | None`` の dispatch (chain は ``ContentFetched``
  時のみ ``extract_content.kiq``)
- ``None`` (重複配送 / 状態不整合) は no-op で exit

設計上の決定:

- ``TemporaryFetchError`` は Service 内で全て catch して
  ``TransientlyDropped`` に変換する (taskiq retry は使わず DB 駆動)
- ``attempt`` は ``pending.attempt_count`` を SSoT として使用 (caller から
  受け取らない、ι.2)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain import Article
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
    PendingHtmlFetch,
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


@dataclass(frozen=True, slots=True)
class ContentFetched:
    """成功 — 永続化済 ``Article`` Entity。caller が ``extract_content.kiq`` に流す。"""

    article: Article


@dataclass(frozen=True, slots=True)
class ConflictLost:
    """別 worker が ``article_url_id`` の article を先に作ったため敗退。

    DB 上は pending を ``closed`` に閉じ、audit ``conflict_lost`` (SKIPPED) を焼く。
    caller は何もしない (winner 側が既に extract_content chain 済)。
    """


@dataclass(frozen=True, slots=True)
class TerminallyDropped:
    """二度試しても無意味な失敗 (URL dead / content unusable / promotion 失敗)。

    ``reason_code`` は ``payload.reason_code`` に焼かれる SQL 集計 key。
    ``permanent_fetch_error`` / ``extraction_empty_<reason>`` /
    ``promotion_<failure_code>`` / ``article_persist_anomaly`` のいずれか。
    """

    reason_code: str


@dataclass(frozen=True, slots=True)
class TransientlyDropped:
    """一時失敗 — caller は retry 不要 (DB 上で next ``ready_at`` まで backoff 済)。

    ``reason_code`` は ``temporary_will_retry_<policy.code>`` (まだ余力あり) または
    ``temporary_exhausted_<policy.code>`` (max_attempts 超過で closed) の 2 系統。
    """

    reason_code: str


ContentFetchOutcome = (
    ContentFetched | ConflictLost | TerminallyDropped | TransientlyDropped
)


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

    async def execute(self, pending_id: int) -> ContentFetchOutcome | None:
        """pending 1 件を HTML 取得 → promotion → 永続化 → 監査までの一連を担う。

        Returns:
            ``None`` — 重複配送 / lease 衝突 / 状態不整合 (静かに exit)。
            それ以外は 4 variant の Outcome を返す。
        """
        t0 = time.monotonic()
        extractor = self._extractor_factory()
        extractor_class = type(extractor).__name__

        # 入口 SELECT: pending 1 行 + JOIN article_urls を 1 SQL で取る
        pending = await self._load(pending_id)
        if pending is None:
            # 既に DELETE 済 (at-least-once 重複配送)
            return None
        if pending.status != "running":
            # cron poller が claim していない (lease 衝突 / 古い message)
            return None

        # HTTP 取得
        try:
            html_result = await extractor.fetch(pending.normalized_url)
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

        # promotion (PendingHtmlFetch + HTML → ReadyForArticle)
        pending_for_advance = self._reconstruct_pending_html_fetch(pending)
        advanced = ReadyForArticle.try_advance_from(
            pending_for_advance,
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

        # 永続化 + audit (race-loss は ConflictLost に変換)
        return await self._persist_and_audit(
            pending=pending,
            advanced=advanced,
            duration_ms=_elapsed_ms(t0),
            extractor_class=extractor_class,
            body_length=len(advanced.body),
        )

    async def _load(self, pending_id: int) -> PendingHtmlContext | None:
        """``pending_html_articles`` 1 行を JOIN ``article_urls`` 込みで SELECT。"""
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
    ) -> TransientlyDropped:
        """一時失敗を per-error policy で捌く。

        ``pending.attempt_count >= policy.max_attempts`` なら ``mark_exhausted``
        (status='closed') + ``dropped_transient`` (FAILED) audit。
        未満なら ``mark_will_retry(ready_at=next_at)`` (status='open' + 未来の
        ready_at) + ``will_retry`` (FAILED) audit。
        """
        policy, delay_minutes = compute_next_delay_minutes(exc, pending.attempt_count)
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)
            error_class_fqn = _fqn(exc)

            if pending.attempt_count >= policy.max_attempts:
                await pending_repo.mark_exhausted(pending.id)
                reason_code = f"temporary_exhausted_{policy.code}"
                payload = ContentFetchPayload(
                    article_url_id=pending.article_url_id,
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
                    source_id=pending.source_id,
                    attempt=pending.attempt_count,
                    duration_ms=duration_ms,
                    error_class=error_class_fqn,
                )
                await session.commit()
                return TransientlyDropped(reason_code=reason_code)

            next_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
            await pending_repo.mark_will_retry(pending.id, ready_at=next_at)
            reason_code = f"temporary_will_retry_{policy.code}"
            payload = ContentFetchPayload(
                article_url_id=pending.article_url_id,
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
                source_id=pending.source_id,
                attempt=pending.attempt_count,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
            )
            await session.commit()
            return TransientlyDropped(reason_code=reason_code)

    async def _handle_terminal(
        self,
        pending: PendingHtmlContext,
        *,
        duration_ms: int,
        extractor_class: str,
        reason_code: str,
        exc: BaseException | None = None,
        quality_gate_metric: dict | None = None,
    ) -> TerminallyDropped:
        """永続失敗を ``closed`` に閉じて ``dropped_terminal`` (SKIPPED) を焼く。"""
        async with self._session_factory() as session:
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)
            error_class_fqn = _fqn(exc) if exc is not None else None

            await pending_repo.mark_terminal(pending.id)
            payload = ContentFetchPayload(
                article_url_id=pending.article_url_id,
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
                source_id=pending.source_id,
                attempt=pending.attempt_count,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
            )
            await session.commit()
            return TerminallyDropped(reason_code=reason_code)

    async def _persist_and_audit(
        self,
        *,
        pending: PendingHtmlContext,
        advanced: ReadyForArticle,
        duration_ms: int,
        extractor_class: str,
        body_length: int,
    ) -> ContentFetched | ConflictLost | TerminallyDropped:
        """``articles`` INSERT + ``pending_html_articles`` DELETE を同 tx で commit。

        race-loss (``save_via_article_url`` が ``None``) → ``find_by_article_url_id``
        で existing を読み戻す。検出ありなら ``ConflictLost`` (audit)、検出なしは
        構造異常として ``TerminallyDropped("article_persist_anomaly")``。
        """
        async with self._session_factory() as session:
            article_repo = ArticleRepository(session)
            pending_repo = PendingHtmlArticleRepository(session)
            event_repo = PipelineEventRepository(session)

            draft = ArticleDraft(
                title=advanced.title,
                body=advanced.body,
                published_at=advanced.published_at,
            )
            persisted = await article_repo.save_via_article_url(
                draft=draft,
                article_url_id=pending.article_url_id,
                source_id=advanced.source_id,
                source_url=advanced.source_url,
            )

            if persisted is None:
                # race-loss: 既存を読み戻し
                existing = await article_repo.find_by_article_url_id(
                    pending.article_url_id
                )
                if existing is None:
                    # 構造異常 (winner が DELETE pending → INSERT article をしたが
                    # その article がもう消えている等、通常は起きない)
                    await pending_repo.mark_terminal(pending.id)
                    payload = ContentFetchPayload(
                        article_url_id=pending.article_url_id,
                        extractor_class=extractor_class,
                        reason_code="article_persist_anomaly",
                    )
                    await event_repo.append(
                        stage=Stage.CONTENT_FETCH,
                        event_type=EventType.SKIPPED,
                        outcome_code="dropped_terminal",
                        payload=payload,
                        source_id=pending.source_id,
                        attempt=pending.attempt_count,
                        duration_ms=duration_ms,
                    )
                    await session.commit()
                    return TerminallyDropped(reason_code="article_persist_anomaly")

                # ConflictLost: pending を削除 + audit
                await pending_repo.delete_one(pending.id)
                payload = ContentFetchPayload(
                    article_url_id=pending.article_url_id,
                    extractor_class=extractor_class,
                    reason_code="conflict_lost",
                )
                await event_repo.append(
                    stage=Stage.CONTENT_FETCH,
                    event_type=EventType.SKIPPED,
                    outcome_code="conflict_lost",
                    payload=payload,
                    source_id=pending.source_id,
                    article_id=existing.id,
                    attempt=pending.attempt_count,
                    duration_ms=duration_ms,
                )
                await session.commit()
                return ConflictLost()

            # 成功: pending DELETE + audit (同 tx)
            await pending_repo.delete_one(pending.id)
            article = Article.from_draft_via_article_url(
                draft,
                id=persisted.id,
                article_url_id=pending.article_url_id,
                created_at=persisted.created_at,
            )
            payload = ContentFetchPayload(
                article_url_id=pending.article_url_id,
                extractor_class=extractor_class,
                body_length=body_length,
            )
            await event_repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.SUCCEEDED,
                outcome_code="fetched",
                payload=payload,
                source_id=pending.source_id,
                article_id=article.id,
                attempt=pending.attempt_count,
                duration_ms=duration_ms,
            )
            await session.commit()
            return ContentFetched(article=article)

    @staticmethod
    def _reconstruct_pending_html_fetch(
        pending: PendingHtmlContext,
    ) -> PendingHtmlFetch:
        """``pending.staged_attributes`` から ``PendingHtmlFetch`` を再構築する。

        ``ReadyForArticle.try_advance_from`` が ``PendingHtmlFetch`` を要求する
        ため (RSS 由来 title/published_at_hint と HTML 由来の merge 規則を
        同所に集約)、Service 入口で復元する。``source_url`` は
        ``article_urls.normalized_url`` を使う (RSS 受信時に canonicalize 済)。
        """
        attrs = pending.staged_attributes
        return PendingHtmlFetch(
            title=attrs.title,
            source_id=pending.source_id,
            source_url=pending.normalized_url,
            published_at_hint=attrs.published_at_hint,
            prefer_html_title=attrs.prefer_html_title,
        )


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
