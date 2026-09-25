"""Stage 2 completion の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BasePipelineEventPayload, CompletionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import exception_fqn
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository
from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)
from app.collection.article_completion.errors import (
    ArticleCompletionRejectedError,
    ArticleExtractionCrashedError,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.http.errors import HttpResponseError, HttpTransportError


class CompletionOutcomeCode(StrEnum):
    """Stage.COMPLETION の outcome code (stage ファイル内定義分のみ)。"""

    ARTICLE_COMPLETED = "article_completed"
    BACKFILL_COMPLETION_AGED_OUT = "backfill_completion_aged_out"


class ArticleCompletionAuditRepository:
    """Stage 2 専用の payload と outcome_code を決める。"""

    STAGE: ClassVar[Stage] = Stage.COMPLETION

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    async def append_consumer_succeeded(
        self,
        *,
        incomplete_article_id: int,
        source_id: int,
        analyzable_article_id: int,
        article: AnalyzableArticle,
    ) -> None:
        """新経路の成功を試行番号に依存せず、記事保存と同じ取引へ追加する。"""
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=CompletionOutcomeCode.ARTICLE_COMPLETED.value,
            payload=CompletionPayload(
                incomplete_article_id=incomplete_article_id,
                canonical_url=str(article.source_url),
                body_length=len(article.body),
            ),
            source_id=source_id,
            article_id=analyzable_article_id,
        )

    async def append_consumer_failed(
        self,
        *,
        incomplete_article_id: int,
        source_id: int | None,
        source_name: str | None,
        exc: Exception,
        decision: RetryArticleCompletion | CloseArticleCompletion,
    ) -> None:
        """工程判断と選択した原因情報だけを監査へ写し、例外の自由文は出力しない。"""
        retry = isinstance(decision, RetryArticleCompletion)
        reason_code: str | None = None
        if isinstance(exc, HttpTransportError):
            reason_code = exc.failure.reason.value
        elif isinstance(exc, ArticleExtractionCrashedError):
            reason_code = exc.reason.value
        await self._append_event(
            event_type=EventType.FAILED if retry else EventType.REJECTED,
            outcome_code=decision.code,
            payload=CompletionPayload(
                incomplete_article_id=incomplete_article_id,
                source_name=source_name,
                failure_action="retry" if retry else "close",
                error_chain=extract_error_chain(exc),
                reason_code=reason_code,
                http_status=exc.status_code
                if isinstance(exc, HttpResponseError)
                else None,
                defects=(
                    [defect.value for defect in exc.defects]
                    if isinstance(exc, ArticleCompletionRejectedError)
                    else None
                ),
            ),
            source_id=source_id,
            error_class=exception_fqn(exc),
            retryability=Retryability.RETRYABLE
            if retry
            else Retryability.NON_RETRYABLE,
        )

    async def append_backfill_completion_aged_out(
        self,
        *,
        incomplete_article_id: int,
        source_id: int,
        source_name: str,
    ) -> None:
        """古い未完成行の補完を救済が打ち切った事実を、closed更新と同じ取引へ残す。"""
        await self._append_event(
            event_type=EventType.REJECTED,
            outcome_code=CompletionOutcomeCode.BACKFILL_COMPLETION_AGED_OUT.value,
            payload=CompletionPayload(
                incomplete_article_id=incomplete_article_id,
                source_name=source_name,
                failure_action="close",
            ),
            source_id=source_id,
            retryability=Retryability.NON_RETRYABLE,
        )

    async def _append_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        await self._events.append(
            stage=self.STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )
