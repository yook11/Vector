"""補完失敗後の ``incomplete_articles`` 状態遷移と audit を行う。"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.error_fields import exception_fqn
from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.scrape_failure import (
    ScrapeFailure,
    ScrapeRetryable,
    ScrapeTerminal,
    classify_scrape_failure,
)

logger = structlog.get_logger(__name__)


class ArticleCompletionFailureHandler:
    """scrape / complete / persist の失敗経路を処理する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle_scrape_failure(
        self,
        ready: ReadyForArticleCompletion,
        failure: ScrapeFailure,
    ) -> None:
        """scrape failure を Retry 軸で closed / retry に振り分ける。"""
        decision = classify_scrape_failure(failure)
        match decision:
            case ScrapeTerminal() as terminal:
                await self._handle_terminal(ready, failure=failure, terminal=terminal)
            case ScrapeRetryable() as retryable:
                await self._handle_temporary(
                    ready, failure=failure, disposition=retryable
                )

    async def handle_completion_rejected(
        self,
        ready: ReadyForArticleCompletion,
        rejection: CompletionRejection,
    ) -> None:
        """complete concern のドメイン拒絶を ``closed`` に閉じる。"""
        canonical_url = ready.source_url
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            updated = await ArticleCompletionRepository(session).close_claimed(
                ready, now=now
            )
            audit = ArticleCompletionAuditRepository(session)
            if updated:
                await audit.append_completion_rejected(ready=ready, rejection=rejection)
            else:
                await audit.append_stale_attempt(ready=ready)
            await session.commit()

        if not updated:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
                attempt_count=ready.attempt_count,
                reason_code=rejection.reason_code,
            )
            return None

        logger.warning(
            "article_completion_rejected",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            canonical_url=str(canonical_url),
            reason_code=rejection.reason_code,
            defects=list(rejection.defect_codes),
        )
        return None

    async def handle_persist_crashed(
        self,
        ready: ReadyForArticleCompletion,
        exc: BaseException,
    ) -> None:
        """persist の DB 例外を別 session で best-effort 監査する。"""
        try:
            async with self._session_factory() as audit_session:
                await ArticleCompletionAuditRepository(
                    audit_session
                ).append_persist_crashed(ready=ready, exc=exc)
                await audit_session.commit()
        except Exception as audit_exc:
            logger.exception(
                "article_completion_persist_audit_dropped",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(ready.source_url),
                business_error_class=(exception_fqn(exc)),
                audit_error_class=(exception_fqn(audit_exc)),
            )

    async def _handle_temporary(
        self,
        ready: ReadyForArticleCompletion,
        *,
        failure: ScrapeFailure,
        disposition: ScrapeRetryable,
    ) -> None:
        """``ScrapeRetryable`` を retry schedule または exhausted close に反映する。"""
        canonical_url = ready.source_url
        exhausted = disposition.is_exhausted(ready.attempt_count)
        now = datetime.now(UTC)
        next_at = disposition.next_ready_at(now=now, attempt_count=ready.attempt_count)
        async with self._session_factory() as session:
            repository = ArticleCompletionRepository(session)
            if exhausted:
                updated = await repository.close_claimed(ready, now=now)
            else:
                updated = await repository.schedule_retry(
                    ready, ready_at=next_at, now=now
                )
            audit = ArticleCompletionAuditRepository(session)
            if updated:
                await audit.append_scrape_outcome(
                    ready=ready, failure=failure, retry_exhausted=exhausted
                )
            else:
                await audit.append_stale_attempt(ready=ready)
            await session.commit()

        if not updated:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
                attempt_count=ready.attempt_count,
                reason_code=disposition.reason_code,
            )
            return None

        logger.warning(
            "article_completion_temporary",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            canonical_url=str(canonical_url),
            reason_code=disposition.reason_code,
            exhausted=exhausted,
            attempt_count=ready.attempt_count,
            max_attempts=disposition.max_attempts,
            next_ready_at=None if exhausted else next_at.isoformat(),
            detail=disposition.detail,
        )
        return None

    async def _handle_terminal(
        self,
        ready: ReadyForArticleCompletion,
        *,
        failure: ScrapeFailure,
        terminal: ScrapeTerminal,
    ) -> None:
        """scrape 終端失敗を ``closed`` に閉じる。"""
        canonical_url = ready.source_url
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            updated = await ArticleCompletionRepository(session).close_claimed(
                ready, now=now
            )
            audit = ArticleCompletionAuditRepository(session)
            if updated:
                await audit.append_scrape_outcome(ready=ready, failure=failure)
            else:
                await audit.append_stale_attempt(ready=ready)
            await session.commit()

        if not updated:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
                attempt_count=ready.attempt_count,
                reason_code=terminal.reason_code,
            )
            return None

        logger.warning(
            "article_completion_scrape_failed",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            canonical_url=str(canonical_url),
            reason_code=terminal.reason_code,
            detail=terminal.detail,
        )
        return None
