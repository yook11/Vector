"""補完失敗後の ``incomplete_articles`` 後処理を行う application service。

failure 後処理を 2 つの concern で別入口に分ける:

- scrape concern (Stage 1, Retry 軸): ``handle_scrape_failure`` が元の
  ``ScrapeFailure`` (5 variant) を受け、内部で ``classify_scrape_failure`` し、
  closed / open+ready_at / exhausted に遷移させる。元の variant は audit まで運ぶ。
- completion concern (Stage 2, Accept 軸): ``handle_completion_rejected`` が
  ``CompletionRejection`` を受け、常に ``closed`` に閉じる (retry は発生しない)。

各 handler は自前 session で状態遷移と audit を**同一 tx**で書く: ``updated=True``
なら本来の outcome、``updated=False`` (他 worker に追い越され失効) なら
``stale_attempt`` を append してから commit する。retry は ``ready_at`` 駆動
(cron poller が再投入)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.audit_repository import (
    ArticleCompletionAuditRepository,
)
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.retry_policy import effective_delay_minutes
from app.collection.article_completion.scrape_failure import (
    Retryable,
    ScrapeFailure,
    Terminal,
    classify_scrape_failure,
)

logger = structlog.get_logger(__name__)


class ArticleCompletionFailureHandler:
    """失敗分類に応じた ``incomplete_articles`` 後処理 + audit を実行する。

    2 入口: ``handle_scrape_failure`` (Stage 1, Retry 軸) と
    ``handle_completion_rejected`` (Stage 2, Accept 軸)。いずれも自前 session で
    状態遷移 + audit (同一 tx) + log を完結させて ``None`` を返す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle_scrape_failure(
        self,
        ready: ReadyForArticleCompletion,
        failure: ScrapeFailure,
    ) -> None:
        """Stage 1 (scrape) 失敗を Retry 軸で捌く。

        元の ``ScrapeFailure`` を受け、内部で ``classify_scrape_failure`` して
        ``Terminal`` → pending を ``closed``、``Retryable`` → policy データ駆動で
        次 ``ready_at`` を計算 (exhausted なら ``closed``)。元の variant は audit の
        payload 組み立て (concern 別 event_type / 構造化列) のため handler 先まで運ぶ。
        """
        decision = classify_scrape_failure(failure)
        match decision:
            case Terminal() as terminal:
                await self._handle_terminal(ready, failure=failure, terminal=terminal)
            case Retryable() as retryable:
                await self._handle_temporary(
                    ready, failure=failure, disposition=retryable
                )

    async def handle_completion_rejected(
        self,
        ready: ReadyForArticleCompletion,
        rejection: CompletionRejection,
    ) -> None:
        """Stage 2 (completion) ドメイン拒絶を ``closed`` に閉じる。

        Accept 軸のため retry は発生しない。``article_completion_rejected`` で
        scrape 失敗とは別ストリームに観測する。
        """
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
            detail=rejection.detail,
        )
        return None

    async def _handle_temporary(
        self,
        ready: ReadyForArticleCompletion,
        *,
        failure: ScrapeFailure,
        disposition: Retryable,
    ) -> None:
        """``Retryable`` を policy データ駆動で捌く。

        ``effective_delay_minutes`` で次回遅延を算出し、``attempt_count >=
        policy.max_attempts`` なら ``closed`` (経路 4)、未満なら ``open`` + 未来の
        ``ready_at`` に戻す (経路 3)。どちらも audit の outcome_code は transport
        理由で共通、give-up は payload ``retry_exhausted`` で表す。
        """
        canonical_url = ready.source_url
        policy = disposition.policy
        delay_minutes = effective_delay_minutes(
            policy,
            retry_after_seconds=disposition.retry_after_seconds,
            attempt_count=ready.attempt_count,
        )
        exhausted = ready.attempt_count >= policy.max_attempts
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            repository = ArticleCompletionRepository(session)
            if exhausted:
                updated = await repository.close_claimed(ready, now=now)
            else:
                next_at = now + timedelta(minutes=delay_minutes)
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
            policy_code=policy.code,
            exhausted=exhausted,
            attempt_count=ready.attempt_count,
            detail=disposition.detail,
        )
        return None

    async def _handle_terminal(
        self,
        ready: ReadyForArticleCompletion,
        *,
        failure: ScrapeFailure,
        terminal: Terminal,
    ) -> None:
        """scrape 終端失敗を ``closed`` に閉じる (経路 2)。

        audit は variant の concern で event_type が分かれる (transport / crash =
        failed、内容棄却 = rejected)。log は分類済 ``terminal`` の reason_code/detail
        で観測する。
        """
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
