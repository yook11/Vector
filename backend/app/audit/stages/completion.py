"""Stage 2 completion の監査イベントを組み立てる。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.prompt_safety import screen_untrusted_text
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CompletionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import (
    error_message_of,
    exception_fqn,
    redacted_audit_message,
)
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_db_failure,
)
from app.audit.injection_signal import record_injection_boundary_detected
from app.audit.repository import PipelineEventRepository
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildError,
    ArticleCompletionReadyBuildFacts,
    ReadyForArticleCompletion,
)
from app.collection.article_completion.repository import (
    CompletionOutcome,
    CompletionSucceeded,
    CompletionSuperseded,
    CompletionUrlConflict,
)
from app.collection.article_completion.scrape_failure import (
    ScrapeContentQualityTooLow,
    ScrapeFailure,
    ScrapeNotHtml,
    ScrapeParseCrashed,
    ScrapeParserGaveUp,
    ScrapeRetryable,
    classify_external_fetch_error,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import (
    CanonicalArticleUrlInvalidError,
)
from app.collection.domain.observed_article import ObservedArticleInvalidError
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.errors import SourceNotRegisteredError

logger = structlog.get_logger(__name__)


class CompletionOutcomeCode(StrEnum):
    """Stage.COMPLETION の outcome code (stage ファイル内定義分のみ)。"""

    ARTICLE_COMPLETED = "article_completed"
    PERSIST_SUPERSEDED = "persist_superseded"
    PERSIST_URL_CONFLICT = "persist_url_conflict"
    PERSIST_CRASHED = "persist_crashed"
    SCRAPE_PARSE_CRASHED = "scrape_parse_crashed"
    SCRAPE_NOT_HTML = "scrape_not_html"
    SCRAPE_PARSER_GAVE_UP = "scrape_parser_gave_up"
    SCRAPE_CONTENT_QUALITY_TOO_LOW = "scrape_content_quality_too_low"
    STALE_ATTEMPT = "stale_attempt"
    READY_BUILD_FAILED_URL_INVALID = "completion_ready_build_failed_url_invalid"
    READY_BUILD_FAILED_OBSERVED_ARTICLE_INVALID = (
        "completion_ready_build_failed_observed_article_invalid"
    )
    READY_BUILD_FAILED_SOURCE_NOT_REGISTERED = (
        "completion_ready_build_failed_source_not_registered"
    )
    READY_BUILD_FAILED_DB_ERROR = "completion_ready_build_failed_db_error"
    READY_BUILD_FAILED_UNEXPECTED_ERROR = (
        "completion_ready_build_failed_unexpected_error"
    )


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
            case CompletionSucceeded(analyzable_article_id=analyzable_article_id):
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.SUCCEEDED,
                    outcome_code=CompletionOutcomeCode.ARTICLE_COMPLETED.value,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        body_length=len(advanced.body),
                    ),
                    source_id=ready.source_id,
                    article_id=analyzable_article_id,
                )
            case CompletionSuperseded():
                await self._append_race_loss(
                    ready=ready, outcome_code=CompletionOutcomeCode.PERSIST_SUPERSEDED
                )
            case CompletionUrlConflict():
                await self._append_race_loss(
                    ready=ready, outcome_code=CompletionOutcomeCode.PERSIST_URL_CONFLICT
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _append_race_loss(
        self, *, ready: ReadyForArticleCompletion, outcome_code: CompletionOutcomeCode
    ) -> None:
        """persist race-loss を skipped として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=outcome_code.value,
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
            case ExternalFetchError() as error:
                projection = self._projection_of_fetch_failed(error)
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
                        error_message=error_message_of(error),
                        error_chain=extract_error_chain(error),
                        retry_exhausted=True if retry_exhausted else None,
                    ),
                    source_id=ready.source_id,
                    error_class=exception_fqn(error),
                    retryability=projection.retryability,
                )
            case ScrapeParseCrashed(
                error_class=error_class, error_message=error_message
            ):
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
                        error_message=redacted_audit_message(error_message),
                    ),
                    source_id=ready.source_id,
                    error_class=error_class,
                    retryability=projection.retryability,
                )
            case ScrapeNotHtml(content_type=content_type):
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=CompletionOutcomeCode.SCRAPE_NOT_HTML,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        content_type=content_type,
                    ),
                )
            case ScrapeParserGaveUp():
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=CompletionOutcomeCode.SCRAPE_PARSER_GAVE_UP,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                    ),
                )
            case ScrapeContentQualityTooLow(
                body_length=body_length,
                title_present=title_present,
                body_sample=body_sample,
            ):
                # 空 sample は body_head=None を保つため screening に流さない。
                screening = screen_untrusted_text(body_sample) if body_sample else None
                injected = screening is not None and screening.injection_detected
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=CompletionOutcomeCode.SCRAPE_CONTENT_QUALITY_TOO_LOW,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        attempt_count=ready.attempt_count,
                        body_length=body_length,
                        quality_gate_metric={"title_present": title_present},
                        # body_head は curation の input_content と同様に無害化して
                        # 焼く (監査 payload を将来 LLM 再投入しても injection を
                        # 持ち込まないため)。
                        body_head=(
                            screening.sanitized if screening is not None else None
                        ),
                        injection_markers_present=injected or None,
                    ),
                )
                # 観測信号 (metric + log) は監査行の永続化後にのみ出す。append が
                # 倒れた場合に signal だけ残る乖離を防ぐ。completion の Ready は
                # article_id を持たないので source_id + canonical_url で対象を辿れる
                # よう log に残す。
                if injected:
                    record_injection_boundary_detected(stage="completion")
                    logger.warning(
                        "audit_injection_boundary_detected",
                        stage="completion",
                        source_id=ready.source_id,
                        canonical_url=canonical_url,
                    )
            case _ as unreachable:
                assert_never(unreachable)

    async def _append_content_rejected(
        self,
        *,
        ready: ReadyForArticleCompletion,
        outcome_code: CompletionOutcomeCode,
        payload: CompletionPayload,
    ) -> None:
        """scrape の内容棄却を rejected として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=outcome_code.value,
            payload=payload,
            source_id=ready.source_id,
        )

    async def append_completion_rejected(
        self,
        *,
        ready: ReadyForArticleCompletion,
        rejection: CompletionRejection,
    ) -> None:
        """complete 段のドメイン不変条件棄却を記録する。

        主 defect を ``outcome_code``、全 defect 集合を ``payload.defects`` に焼く。
        free-text の error_message / error_class は持たない (構造的に PII-free)。
        写像漏れ (``unmapped`` 非空) は ``quality_gate_metric`` に痕跡を残す。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=rejection.reason_code,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
                defects=list(rejection.defect_codes),
                quality_gate_metric=(
                    {"unmapped_validation_errors": list(rejection.unmapped)}
                    if rejection.unmapped
                    else None
                ),
            ),
            source_id=ready.source_id,
        )

    async def append_stale_attempt(self, *, ready: ReadyForArticleCompletion) -> None:
        """失効した claim の後処理を skipped として記録する。"""
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=CompletionOutcomeCode.STALE_ATTEMPT.value,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                attempt_count=ready.attempt_count,
            ),
            source_id=ready.source_id,
        )

    async def append_ready_build_error(
        self,
        *,
        pending_id: int,
        exc: Exception,
        facts: ArticleCompletionReadyBuildFacts | None = None,
    ) -> None:
        """Ready 構築不能の typed error / fallback error を記録する。"""
        projection = _project_ready_build_error(exc)
        payload = CompletionPayload(
            failure_kind=(
                projection.failure_kind
                if projection.event_type is EventType.FAILED
                else None
            ),
            pending_id=pending_id,
            pending_status=facts.status if facts is not None else None,
            source_name=str(facts.source_name) if facts is not None else None,
            canonical_url=facts.source_url if facts is not None else None,
            attempt_count=facts.attempt_count if facts is not None else None,
            error_message=(
                error_message_of(exc)
                if projection.event_type is EventType.FAILED
                else None
            ),
            error_chain=(
                extract_error_chain(exc)
                if projection.event_type is EventType.FAILED
                else None
            ),
            reason_code=projection.reason_code,
        )
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=projection.event_type,
            outcome_code=projection.code,
            payload=payload,
            source_id=facts.source_id if facts is not None else None,
            error_class=(
                exception_fqn(exc)
                if projection.event_type is EventType.FAILED
                else None
            ),
            retryability=(
                Retryability.UNKNOWN
                if projection.event_type is EventType.FAILED
                else None
            ),
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
                error_message=error_message_of(exc),
                error_chain=extract_error_chain(exc),
            ),
            source_id=ready.source_id,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )

    @staticmethod
    def _projection_of_fetch_failed(error: ExternalFetchError) -> FailureProjection:
        """transport (origin) 失敗を Stage 2 の失敗属性へ投影する。"""
        decision = classify_external_fetch_error(error)
        retryability = (
            Retryability.RETRYABLE
            if isinstance(decision, ScrapeRetryable)
            else Retryability.NON_RETRYABLE
        )
        return FailureProjection(
            failure_kind="external_fetch",
            retryability=retryability,
            failure_action=None,
            code=error.CODE,
        )

    @staticmethod
    def _projection_of_parse_crashed() -> FailureProjection:
        """parser crash を Stage 2 内部故障として投影する。"""
        return FailureProjection(
            failure_kind=CompletionOutcomeCode.SCRAPE_PARSE_CRASHED.value,
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code=CompletionOutcomeCode.SCRAPE_PARSE_CRASHED.value,
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
                code=CompletionOutcomeCode.PERSIST_CRASHED.value,
            )
        return FailureProjection(
            failure_kind=CompletionOutcomeCode.PERSIST_CRASHED.value,
            retryability=Retryability.UNKNOWN,
            failure_action=None,
            code=CompletionOutcomeCode.PERSIST_CRASHED.value,
        )


@dataclass(frozen=True, slots=True)
class _ReadyBuildErrorProjection:
    event_type: EventType
    code: str
    failure_kind: str | None
    # failure_kind は粗い集計タグ、reason_code は VO が掴んだ機械可読な細分
    reason_code: str | None = None


def _project_ready_build_error(exc: Exception) -> _ReadyBuildErrorProjection:
    if isinstance(exc, ArticleCompletionReadyBuildError):
        return _ReadyBuildErrorProjection(
            event_type=exc.EVENT_TYPE,
            code=exc.CODE,
            failure_kind=exc.FAILURE_KIND,
        )
    if isinstance(exc, CanonicalArticleUrlInvalidError):
        return _ReadyBuildErrorProjection(
            event_type=EventType.FAILED,
            failure_kind="url_invalid",
            code=CompletionOutcomeCode.READY_BUILD_FAILED_URL_INVALID.value,
            reason_code=exc.reason,
        )
    if isinstance(exc, ObservedArticleInvalidError):
        return _ReadyBuildErrorProjection(
            event_type=EventType.FAILED,
            failure_kind="observed_article_invalid",
            code=CompletionOutcomeCode.READY_BUILD_FAILED_OBSERVED_ARTICLE_INVALID.value,
            reason_code=exc.reason,
        )
    if isinstance(exc, SourceNotRegisteredError):
        return _ReadyBuildErrorProjection(
            event_type=EventType.FAILED,
            failure_kind="source_not_registered",
            code=CompletionOutcomeCode.READY_BUILD_FAILED_SOURCE_NOT_REGISTERED.value,
        )
    if isinstance(exc, SQLAlchemyError):
        return _ReadyBuildErrorProjection(
            event_type=EventType.FAILED,
            failure_kind="db_error",
            code=CompletionOutcomeCode.READY_BUILD_FAILED_DB_ERROR.value,
        )
    return _ReadyBuildErrorProjection(
        event_type=EventType.FAILED,
        failure_kind="unexpected_error",
        code=CompletionOutcomeCode.READY_BUILD_FAILED_UNEXPECTED_ERROR.value,
    )
