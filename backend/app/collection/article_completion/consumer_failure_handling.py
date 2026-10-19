"""補完の失敗判断に従って終了を確定し、失敗監査を別に保存する。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_completion.consumer_failure_classification import (
    classify_completion_failure,
)
from app.collection.article_completion.consumer_repository import (
    ArticleCompletionConsumerRepository,
    RecordedIncompleteArticle,
)
from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import ArticleCompletionError
from app.collection.article_completion.metrics import (
    record_completion_processing_outcome,
)


class ArticleCompletionConsumerFailureHandler:
    """監査の二次障害で元の失敗と確定済み状態を置き換えない。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        exc: ArticleCompletionError,
        *,
        incomplete_article_id: int,
        incomplete: RecordedIncompleteArticle | None,
    ) -> RetryCompletion | NoRetryCompletion:
        now = datetime.now(UTC)
        decision = classify_completion_failure(exc, now=now)
        if isinstance(decision, NoRetryCompletion):
            try:
                async with self._session_factory() as session:
                    closed = await ArticleCompletionConsumerRepository(
                        session
                    ).close_nonclosed(incomplete_article_id, now=now)
                    if not closed:
                        return NoRetryCompletion(cause="superseded")
                    await session.commit()
            except Exception as close_error:
                exc = ArticleCompletionError(cause=close_error)
                decision = classify_completion_failure(exc, now=datetime.now(UTC))
                # closed未確定で受信完了にすると、行が開いたままメッセージが消える。
                if isinstance(decision, NoRetryCompletion):
                    decision = RetryCompletion(
                        error=exc,
                        code=decision.code or "unknown",
                        requires_investigation=True,
                    )

        try:
            record_completion_processing_outcome("failed")
        except Exception:  # noqa: S110
            # 計測の障害は監査と同じく元の結果を変えない。
            pass
        try:
            async with self._session_factory() as session:
                await ArticleCompletionAuditRepository(session).append_consumer_failed(
                    incomplete_article_id=incomplete_article_id,
                    source_id=incomplete.source_id if incomplete is not None else None,
                    source_name=(
                        str(incomplete.source_name) if incomplete is not None else None
                    ),
                    exc=exc,
                    decision=decision,
                )
                await session.commit()
        except Exception:  # noqa: S110
            # 失敗監査の障害は元の例外・待機時刻・確定済みclosedを変えない。
            pass
        return decision
