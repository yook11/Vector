"""補完の失敗判断に従って終了を確定する。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_completion.consumer_failure_classification import (
    classify_completion_failure,
)
from app.collection.article_completion.consumer_repository import (
    ArticleCompletionConsumerRepository,
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
    """計測の二次障害で元の失敗と確定済み状態を置き換えない。"""

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
            # 計測の障害は元の結果を変えない。
            pass
        return decision
