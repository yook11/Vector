"""Stage 2 completion の監査イベントを組み立てる。"""

from __future__ import annotations

from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CompletionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_db_failure,
)
from app.audit.repository import PipelineEventRepository
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import (
    CompletionOutcome,
    CompletionSucceeded,
    CompletionSuperseded,
    CompletionUrlConflict,
)
from app.collection.article_completion.scrape_failure import (
    ContentQualityTooLow,
    FetchFailed,
    NotHtml,
    ParseCrashed,
    ParserGaveUp,
    Retryable,
    ScrapeFailure,
    classify_external_fetch_error,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

_ARTICLE_COMPLETED = "article_completed"
_PERSIST_SUPERSEDED = "persist_superseded"
_PERSIST_URL_CONFLICT = "persist_url_conflict"
_PERSIST_CRASHED = "persist_crashed"
_SCRAPE_PARSE_CRASHED = "scrape_parse_crashed"
_SCRAPE_NOT_HTML = "scrape_not_html"
_SCRAPE_PARSER_GAVE_UP = "scrape_parser_gave_up"
_SCRAPE_CONTENT_QUALITY_TOO_LOW = "scrape_content_quality_too_low"
_STALE_ATTEMPT = "stale_attempt"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _redacted(message: str) -> str | None:
    """secret を mask し上限で切り詰める。"""
    return redact_secrets(message)[:_ERROR_MESSAGE_LIMIT] or None


class ArticleCompletionAuditRepository:
    """Stage 2 専用の payload / outcome_code / failure projection を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    async def append_persist_outcome(
        self,
        *,
        ready: ReadyForArticleCompletion,
        outcome: CompletionOutcome,
        advanced: AnalyzableArticle,
    ) -> None:
        """persist outcome を記録する。"""
        canonical_url = str(ready.source_url)
        match outcome:
            case CompletionSucceeded(article_id=article_id):
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.SUCCEEDED,
                    outcome_code=_ARTICLE_COMPLETED,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        body_length=len(advanced.body),
                    ),
                    source_id=ready.source_id,
                    article_id=article_id,
                )
            case CompletionSuperseded():
                await self._append_race_loss(
                    ready=ready, outcome_code=_PERSIST_SUPERSEDED
                )
            case CompletionUrlConflict():
                await self._append_race_loss(
                    ready=ready, outcome_code=_PERSIST_URL_CONFLICT
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _append_race_loss(
        self, *, ready: ReadyForArticleCompletion, outcome_code: str
    ) -> None:
        """persist race-loss を skipped として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=outcome_code,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
            ),
            source_id=ready.source_id,
        )

    async def append_scrape_outcome(
        self,
        *,
        ready: ReadyForArticleCompletion,
        failure: ScrapeFailure,
        retry_exhausted: bool = False,
    ) -> None:
        """scrape outcome を failed / rejected として記録する。"""
        canonical_url = str(ready.source_url)
        match failure:
            case FetchFailed(error=error):
                projection = self._projection_of_fetch_failed(failure)
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code=projection.code,
                    payload=CompletionPayload(
                        failure_kind=projection.failure_kind,
                        failure_action=failure_action_value(projection),
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        http_status=getattr(error, "status_code", None),
                        error_message=_redacted(str(error)),
                        error_chain=extract_error_chain(error),
                        retry_exhausted=True if retry_exhausted else None,
                    ),
                    source_id=ready.source_id,
                    error_class=_fqn(error),
                    retryability=projection.retryability,
                )
            case ParseCrashed(error_class=error_class, error_message=error_message):
                projection = self._projection_of_parse_crashed()
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code=projection.code,
                    payload=CompletionPayload(
                        failure_kind=projection.failure_kind,
                        failure_action=failure_action_value(projection),
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        error_message=_redacted(error_message),
                    ),
                    source_id=ready.source_id,
                    error_class=error_class,
                    retryability=projection.retryability,
                )
            case NotHtml(content_type=content_type):
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=_SCRAPE_NOT_HTML,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        content_type=content_type,
                    ),
                )
            case ParserGaveUp():
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=_SCRAPE_PARSER_GAVE_UP,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                    ),
                )
            case ContentQualityTooLow(
                body_length=body_length,
                title_present=title_present,
                body_sample=body_sample,
            ):
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=_SCRAPE_CONTENT_QUALITY_TOO_LOW,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        body_length=body_length,
                        quality_gate_metric={"title_present": title_present},
                        body_head=body_sample,
                    ),
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _append_content_rejected(
        self,
        *,
        ready: ReadyForArticleCompletion,
        outcome_code: str,
        payload: CompletionPayload,
    ) -> None:
        """scrape の内容棄却を rejected として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=outcome_code,
            payload=payload,
            source_id=ready.source_id,
        )

    async def append_completion_rejected(
        self,
        *,
        ready: ReadyForArticleCompletion,
        rejection: CompletionRejection,
    ) -> None:
        """complete 段のドメイン不変条件棄却を記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=rejection.reason_code,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
                error_message=(
                    _redacted(rejection.error_message)
                    if rejection.error_message is not None
                    else None
                ),
            ),
            source_id=ready.source_id,
            error_class=rejection.error_class,
        )

    async def append_stale_attempt(self, *, ready: ReadyForArticleCompletion) -> None:
        """失効した claim の後処理を skipped として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=_STALE_ATTEMPT,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
            ),
            source_id=ready.source_id,
        )

    async def append_persist_crashed(
        self, *, ready: ReadyForArticleCompletion, exc: BaseException
    ) -> None:
        """persist 段で rollback された例外を別 tx の failed として記録する。"""
        projection = self._projection_of_persist_crash(exc)
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=CompletionPayload(
                failure_kind=projection.failure_kind,
                failure_action=failure_action_value(projection),
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
                error_message=_redacted(str(exc)),
                error_chain=extract_error_chain(exc),
            ),
            source_id=ready.source_id,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    @staticmethod
    def _projection_of_fetch_failed(failure: FetchFailed) -> FailureProjection:
        """``FetchFailed`` を Stage 2 の失敗属性へ投影する。"""
        decision = classify_external_fetch_error(failure.error)
        retryability = (
            Retryability.RETRYABLE
            if isinstance(decision, Retryable)
            else Retryability.NON_RETRYABLE
        )
        return FailureProjection(
            failure_kind="external_fetch",
            retryability=retryability,
            failure_action=None,
            code=failure.error.CODE,
        )

    @staticmethod
    def _projection_of_parse_crashed() -> FailureProjection:
        """parser crash を Stage 2 内部故障として投影する。"""
        return FailureProjection(
            failure_kind=_SCRAPE_PARSE_CRASHED,
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code=_SCRAPE_PARSE_CRASHED,
        )

    @staticmethod
    def _projection_of_persist_crash(exc: BaseException) -> FailureProjection:
        """persist crash を DB adapter / catch-all から projection する。"""
        db_projection = project_db_failure(exc)
        if db_projection is not None:
            return FailureProjection(
                failure_kind=db_projection.failure_kind,
                retryability=db_projection.retryability,
                failure_action=None,
                code=_PERSIST_CRASHED,
            )
        return FailureProjection(
            failure_kind=_PERSIST_CRASHED,
            retryability=Retryability.UNKNOWN,
            failure_action=None,
            code=_PERSIST_CRASHED,
        )
