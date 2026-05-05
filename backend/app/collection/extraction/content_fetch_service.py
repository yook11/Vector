"""Stage 2 (content_fetch) のビジネスロジックを集約する Service。

``extract_html_body`` task の中身 (HTTP 取得 / 抽出 / promotion / 永続化 /
監査) を集約し、taskiq retry policy だけ task に残す。Stage 1
``IngestionService`` と同じ「入口 task pattern」。

Service の責務:

- HTTP 取得 (``ArticleHtmlExtractor`` 経由)
- ``ExtractionEmpty`` / promotion ``Failed`` / race-lost の捌き
- ``Article`` 永続化 (``ArticleRepository`` 経由)
- ``pipeline_events`` への監査書込

task の責務 (本 Service の外):

- ``TemporaryFetchError`` の retry/drop 判定 (``is_last_attempt``)
- ``Outcome`` の dispatch (成功時のみ ``extract_content.kiq``)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

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
from app.collection.ingestion.domain.fetched_article import (
    Failed as IngestionFailed,
)
from app.collection.ingestion.domain.fetched_article import (
    ReadyForArticle,
)
from app.collection.ingestion.staged import StagedArticle
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import ContentFetchPayload
from app.observability.repository import PipelineEventRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ContentFetched:
    """成功 — 永続化済 ``Article`` Entity (race-lost で既存を読み戻した場合も含む)。"""

    article: Article


@dataclass(frozen=True, slots=True)
class TerminallyDropped:
    """二度試しても無意味な失敗 (URL dead / content unusable)。

    ``reason_code`` は ``payload.reason_code`` に焼かれる SQL 集計 key。
    ``permanent_fetch_error`` / ``extraction_empty_<reason>`` /
    ``promotion_<failure_code>`` / ``article_persist_failed`` のいずれか。
    """

    reason_code: str


@dataclass(frozen=True, slots=True)
class TransientlyDropped:
    """taskiq retry budget を使い切った失敗 (次 cron で価値あり)。

    ``audit_exhausted`` 経由でのみ生成される (現状 ``execute`` から直接は
    返らない、``execute`` 内の transient は raise で task に伝播する)。
    """

    reason_code: str


ContentFetchOutcome = ContentFetched | TerminallyDropped | TransientlyDropped


class ContentFetchService:
    """Pattern H 2 段目 — staged article 1 件を HTML 取得 + 永続化する。

    ``TemporaryFetchError`` は **catch せず raise** で caller (task) に
    伝播する (retry policy は taskiq broker の文脈なので task の責務、
    Service に持ち込まない)。それ以外の失敗は Service 内で audit + 戻り値で完結する。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        extractor_factory: Callable[[], ArticleHtmlExtractor] = ArticleHtmlExtractor,
    ) -> None:
        self._session_factory = session_factory
        self._extractor_factory = extractor_factory

    async def execute(
        self,
        staged: StagedArticle,
        *,
        attempt: int,
    ) -> ContentFetchOutcome:
        """staged 1 件を HTML 取得 → promotion → 永続化 → 監査までの一連を担う。

        ``TemporaryFetchError`` は raise (caller が ``is_last_attempt`` で判断)。
        """
        t0 = time.monotonic()
        pending = staged.pending
        extractor = self._extractor_factory()
        extractor_class = type(extractor).__name__

        # HTTP 取得 — TemporaryFetchError は素通し
        try:
            html_result = await extractor.fetch(pending.source_url)
        except PermanentFetchError as e:
            await self._audit_terminal(
                discovered_id=staged.discovered_id,
                attempt=attempt,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code="permanent_fetch_error",
                exc=e,
            )
            return TerminallyDropped(reason_code="permanent_fetch_error")

        # ExtractionEmpty
        if isinstance(html_result, ExtractionEmpty):
            reason_code = f"extraction_empty_{html_result.reason}"
            await self._audit_terminal(
                discovered_id=staged.discovered_id,
                attempt=attempt,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code=reason_code,
            )
            return TerminallyDropped(reason_code=reason_code)

        # 静的型 narrow (ExtractedContent | ExtractionEmpty の残り)
        assert isinstance(html_result, ExtractedContent)  # noqa: S101

        # promotion (PendingHtmlFetch + HTML → ReadyForArticle)
        advanced = ReadyForArticle.try_advance_from(
            pending,
            body=html_result.body,
            html_published_at=html_result.published_at,
            html_title=html_result.title,
        )
        if isinstance(advanced, IngestionFailed):
            reason_code = f"promotion_{advanced.reason.code}"
            await self._audit_terminal(
                discovered_id=staged.discovered_id,
                attempt=attempt,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code=reason_code,
                quality_gate_metric={
                    "body_length": len(html_result.body),
                    "failure_detail": advanced.reason.detail,
                },
            )
            return TerminallyDropped(reason_code=reason_code)

        # 永続化 (race-lost recovery 込み)
        article = await self._persist(advanced, staged.discovered_id)
        if article is None:
            await self._audit_terminal(
                discovered_id=staged.discovered_id,
                attempt=attempt,
                duration_ms=_elapsed_ms(t0),
                extractor_class=extractor_class,
                reason_code="article_persist_failed",
            )
            return TerminallyDropped(reason_code="article_persist_failed")

        # 成功
        await self._audit_success(
            discovered_id=staged.discovered_id,
            article_id=article.id,
            attempt=attempt,
            duration_ms=_elapsed_ms(t0),
            extractor_class=extractor_class,
            body_length=len(advanced.body),
        )
        return ContentFetched(article=article)

    async def audit_exhausted(
        self,
        staged: StagedArticle,
        *,
        attempt: int,
        exc: TemporaryFetchError,
    ) -> None:
        """task が ``is_last_attempt`` 検知時に呼ぶ。

        ``TransientlyDropped`` 相当の audit を別 session で焼く。retry budget
        を使い切った後の最終 attempt の記録。
        """
        error_class_fqn = _fqn(exc)
        async with self._session_factory() as session:
            repo = PipelineEventRepository(session)
            payload = ContentFetchPayload(
                discovered_article_id=staged.discovered_id,
                extractor_class=type(self._extractor_factory()).__name__,
                reason_code="temporary_fetch_error_exhausted",
                error_message=str(exc)[:500],
                error_chain=[error_class_fqn],
            )
            await repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.FAILED,
                outcome_code="dropped_transient",
                payload=payload,
                attempt=attempt,
                duration_ms=None,
                error_class=error_class_fqn,
            )
            await session.commit()

    async def _persist(
        self,
        advanced: ReadyForArticle,
        discovered_id: int,
    ) -> Article | None:
        """``advanced`` を ``articles`` に永続化。race-lost なら既存を読み戻す。"""
        async with self._session_factory() as session:
            article_repo = ArticleRepository(session)
            draft = ArticleDraft(
                title=advanced.title,
                body=advanced.body,
                published_at=advanced.published_at,
            )
            persisted = await article_repo.save(
                draft=draft,
                discovered_article_id=discovered_id,
                source_id=advanced.source_id,
                source_url=advanced.source_url,
            )
            if persisted is not None:
                await session.commit()
                return Article.from_draft(
                    draft,
                    id=persisted.id,
                    discovered_article_id=discovered_id,
                    created_at=persisted.created_at,
                )

            # race-lost: 既存を読み戻す
            existing = await article_repo.find_by_discovered_article_id(discovered_id)
            await session.commit()
            return existing

    async def _audit_success(
        self,
        *,
        discovered_id: int,
        article_id: int,
        attempt: int,
        duration_ms: int,
        extractor_class: str,
        body_length: int,
    ) -> None:
        async with self._session_factory() as session:
            repo = PipelineEventRepository(session)
            payload = ContentFetchPayload(
                discovered_article_id=discovered_id,
                extractor_class=extractor_class,
                body_length=body_length,
            )
            await repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.SUCCEEDED,
                outcome_code="fetched",
                payload=payload,
                article_id=article_id,
                attempt=attempt,
                duration_ms=duration_ms,
            )
            await session.commit()

    async def _audit_terminal(
        self,
        *,
        discovered_id: int,
        attempt: int,
        duration_ms: int,
        extractor_class: str,
        reason_code: str,
        exc: BaseException | None = None,
        quality_gate_metric: dict | None = None,
    ) -> None:
        error_class_fqn = _fqn(exc) if exc is not None else None
        async with self._session_factory() as session:
            repo = PipelineEventRepository(session)
            payload = ContentFetchPayload(
                discovered_article_id=discovered_id,
                extractor_class=extractor_class,
                reason_code=reason_code,
                error_message=str(exc)[:500] if exc is not None else None,
                error_chain=[error_class_fqn] if error_class_fqn else None,
                quality_gate_metric=quality_gate_metric,
            )
            await repo.append(
                stage=Stage.CONTENT_FETCH,
                event_type=EventType.SKIPPED,
                outcome_code="dropped_terminal",
                payload=payload,
                attempt=attempt,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
            )
            await session.commit()


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
