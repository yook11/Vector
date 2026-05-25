"""Stage 2 (article_completion) 専用の ``pipeline_events`` 監査リポジトリ。

audit row の shape SSoT。Service / FailureHandler は本 class の semantic method
を呼ぶだけで、``CompletionPayload`` の組み立て・``PipelineEventRepository.append``
の引数列・error_chain の FQN を知らない。

tx 境界は呼出側が握る (本 class は ``session.commit()`` を呼ばない)。成功 / skip /
失敗 audit は状態遷移と同一 tx で書き、persist crash (経路 9) のみ呼出側が別 session
を開いて commit する (同一 tx だと audit ごと rollback して痕跡が消えるため)。

``event_type`` は concern 軸 (技術 / transport 故障 = FAILED / 内容棄却 = REJECTED)、
``error_class`` は mechanism 軸 (raise された例外型のみ非 None) で、2 軸は独立。詳細は
``specs/pipeline-events-stage2-completion.md``。
"""

from __future__ import annotations

from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CompletionPayload
from app.audit.error_chain import extract_error_chain
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
    ScrapeFailure,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

# outcome_code 語彙 (audit 契約の SSoT)。prefix が内部サブ段階を表す:
# ``article_completed`` / ``persist_*`` = persist 段、``scrape_*`` = scrape 段、
# ``stale_attempt`` = lifecycle 軸 (prefix なし)。
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
    """secret を mask し上限で切詰。空なら None (foundation 共通の畳み)。"""
    return redact_secrets(message)[:_ERROR_MESSAGE_LIMIT] or None


class ArticleCompletionAuditRepository:
    """Stage 2 (completion) 監査 row の semantic API。"""

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
        """persist 段 (Stage 3) の 3 outcome を記録する (経路 1 / 6 / 7)。"""
        canonical_url = str(ready.source_url)
        match outcome:
            case CompletionSucceeded(article_id=article_id):
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.SUCCEEDED,
                    outcome_code=_ARTICLE_COMPLETED,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        body_length=len(advanced.body),
                    ),
                    source_id=ready.source_id,
                    article_id=article_id,
                    attempt=ready.attempt_count,
                    code=_ARTICLE_COMPLETED,
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
        """persist race-loss (経路 6 / 7) を skipped で記録する。

        完成 body は破棄されるので焼かない。article_id は勝者の id を捏造せず None。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=outcome_code,
            payload=CompletionPayload(canonical_url=str(ready.source_url)),
            source_id=ready.source_id,
            attempt=ready.attempt_count,
            code=outcome_code,
        )

    async def append_scrape_outcome(
        self,
        *,
        ready: ReadyForArticleCompletion,
        failure: ScrapeFailure,
        retry_exhausted: bool = False,
    ) -> None:
        """scrape 段 (Stage 1) の失敗を記録する (経路 2 / 3 / 4)。

        event_type は variant の concern で決まる: transport / crash = FAILED、
        内容棄却 = REJECTED。``retry_exhausted`` は ``FetchFailed`` (唯一の retryable
        source) で give-up したときだけ True を payload に書く。
        """
        canonical_url = str(ready.source_url)
        match failure:
            case FetchFailed(error=error):
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code=error.CODE,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        http_status=getattr(error, "status_code", None),
                        error_message=_redacted(str(error)),
                        error_chain=extract_error_chain(error),
                        retry_exhausted=True if retry_exhausted else None,
                    ),
                    source_id=ready.source_id,
                    attempt=ready.attempt_count,
                    error_class=_fqn(error),
                    code=error.CODE,
                )
            case ParseCrashed(error_class=error_class, error_message=error_message):
                await self._events.append(
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code=_SCRAPE_PARSE_CRASHED,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        error_message=_redacted(error_message),
                    ),
                    source_id=ready.source_id,
                    attempt=ready.attempt_count,
                    error_class=error_class,
                    code=_SCRAPE_PARSE_CRASHED,
                )
            case NotHtml(content_type=content_type):
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=_SCRAPE_NOT_HTML,
                    payload=CompletionPayload(
                        canonical_url=canonical_url,
                        content_type=content_type,
                    ),
                )
            case ParserGaveUp():
                await self._append_content_rejected(
                    ready=ready,
                    outcome_code=_SCRAPE_PARSER_GAVE_UP,
                    payload=CompletionPayload(canonical_url=canonical_url),
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
        """scrape の内容棄却 (NotHtml / ParserGaveUp / ContentQualityTooLow) を
        rejected で記録する。例外を伴わない値判定なので error_class は None。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=outcome_code,
            payload=payload,
            source_id=ready.source_id,
            attempt=ready.attempt_count,
            code=outcome_code,
        )

    async def append_completion_rejected(
        self,
        *,
        ready: ReadyForArticleCompletion,
        rejection: CompletionRejection,
    ) -> None:
        """complete 段 (Stage 2) のドメイン不変条件棄却を記録する (経路 5)。

        scrape の内容棄却と同じ rejected ストリームだが、raise された例外
        (Pydantic ValidationError) を捕えているので error_class を残す。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.REJECTED,
            outcome_code=rejection.reason_code,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                error_message=(
                    _redacted(rejection.error_message)
                    if rejection.error_message is not None
                    else None
                ),
            ),
            source_id=ready.source_id,
            attempt=ready.attempt_count,
            error_class=rejection.error_class,
            code=rejection.reason_code,
        )

    async def append_stale_attempt(self, *, ready: ReadyForArticleCompletion) -> None:
        """失効した attempt の void な後処理を skipped で記録する (経路 8)。

        lifecycle 軸 (attempt 超越) のイベントで単一サブ段階に紐づかないため
        prefix なしの ``stale_attempt``。authoritative なのは勝者の outcome。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.SKIPPED,
            outcome_code=_STALE_ATTEMPT,
            payload=CompletionPayload(canonical_url=str(ready.source_url)),
            source_id=ready.source_id,
            attempt=ready.attempt_count,
            code=_STALE_ATTEMPT,
        )

    async def append_persist_crashed(
        self, *, ready: ReadyForArticleCompletion, exc: BaseException
    ) -> None:
        """persist 段の真の DB 例外を failed で記録する (経路 9)。

        呼出側が **別 session** を開いて本 method を呼び commit する (同一 tx だと
        audit ごと rollback して痕跡が消えるため)。本 method 自体は commit しない。
        """
        await self._events.append(
            stage=Stage.COMPLETION,
            event_type=EventType.FAILED,
            outcome_code=_PERSIST_CRASHED,
            payload=CompletionPayload(
                canonical_url=str(ready.source_url),
                error_message=_redacted(str(exc)),
                error_chain=extract_error_chain(exc),
            ),
            source_id=ready.source_id,
            attempt=ready.attempt_count,
            error_class=_fqn(exc),
            code=_PERSIST_CRASHED,
        )
