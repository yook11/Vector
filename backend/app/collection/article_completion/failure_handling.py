"""Stage 2 (``ArticleCompletionService``) の失敗後処理を実行する application service。

Stage 2 の失敗分類 (``CompletionDisposition`` = ``Terminal`` | ``Retryable``) を
``pending_html_articles`` の状態遷移 (closed / open+ready_at / exhausted) +
構造化ログに対応づける**唯一の場所**。disposition は「分類」のみ、本 handler は
「状態遷移 + log」のみ、``ArticleCompletionService`` は「成功主線」のみ、と責務を
型/ファイルで分離する。

Stage 3 (``ExtractionFailureHandler``) は raw exception を受けて marker dispatch
するが、本 handler は **既に分類済の ``CompletionDisposition`` を受ける** —
Stage 2 は分類を別層 (``disposition.py``) に持つため。実装が似ていても解いて
いる問題が違うので Handler は共有しない。

Stage 2 の retry は ``pending_html_articles.ready_at`` 駆動 (cron poller が
再投入) で taskiq retry に依存しないため、戻り値は ``-> None`` (副作用完結)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.disposition import (
    CompletionDisposition,
    Retryable,
    Terminal,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.retry_policy import effective_delay_minutes

logger = structlog.get_logger(__name__)


class ArticleCompletionFailureHandler:
    """Stage 2 失敗分類に応じた ``pending_html_articles`` 後処理を実行する。

    ``handle`` が単一エントリポイント。``CompletionDisposition`` を受け取り
    副作用 (状態遷移 + log) を完結させて ``None`` を返す。caller (Service /
    transitional persist) は分類して委譲するだけ。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        ready: ReadyForArticleCompletion,
        disposition: CompletionDisposition,
        *,
        exc: BaseException | None = None,
    ) -> None:
        """全失敗を disposition trichotomy の 1 経路に集約する。

        ``Terminal`` → pending を ``closed``。``Retryable`` → policy データ駆動で
        次 ``ready_at`` を計算 (exhausted なら ``closed``)。policy ごとの
        コード分岐は持たず ``exhausted`` 判定だけで経路を 1 本化する。
        """
        match disposition:
            case Terminal() as terminal:
                await self._handle_terminal(
                    ready,
                    reason_code=terminal.reason_code,
                    detail=terminal.detail,
                    exc=exc,
                )
            case Retryable() as retryable:
                await self._handle_temporary(ready, disposition=retryable, exc=exc)

    async def _handle_temporary(
        self,
        ready: ReadyForArticleCompletion,
        *,
        disposition: Retryable,
        exc: BaseException | None = None,
    ) -> None:
        """``Retryable`` を policy データ駆動で捌く。

        ``effective_delay_minutes`` で次回遅延を算出し、``attempt_count >=
        policy.max_attempts`` なら ``closed``、未満なら ``open`` + 未来の
        ``ready_at`` に戻す。policy 別のコード分岐は持たない。
        """
        canonical_url = ready.incomplete_article.source_url
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
            error_class=type(exc).__name__ if exc is not None else None,
        )
        return None

    async def _handle_terminal(
        self,
        ready: ReadyForArticleCompletion,
        *,
        reason_code: str,
        exc: BaseException | None = None,
        detail: str | None = None,
    ) -> None:
        """終端失敗を ``closed`` に閉じる。"""
        canonical_url = ready.incomplete_article.source_url
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            updated = await ArticleCompletionRepository(session).close_claimed(
                ready, now=now
            )
            await session.commit()

        if not updated:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
                attempt_count=ready.attempt_count,
                reason_code=reason_code,
            )
            return None

        logger.warning(
            "article_completion_terminal",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            canonical_url=str(canonical_url),
            reason_code=reason_code,
            error_class=type(exc).__name__ if exc is not None else None,
            detail=detail,
        )
        return None
