"""未完成記事の補完を進め、DBで確定した結果を呼び出し元へ返す。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_completion.article_fetch import fetch_article_response
from app.collection.article_completion.consumer_failure_handling import (
    ArticleCompletionConsumerFailureHandler,
)
from app.collection.article_completion.consumer_repository import (
    ArticleCompletionConsumerRepository,
    RecordedIncompleteArticle,
)
from app.collection.article_completion.consumer_result import (
    CompletionSucceeded,
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.html_completion import complete_with_html
from app.collection.article_completion.html_extraction import extract_html_content
from app.collection.article_completion.metrics import (
    record_completion_processing_outcome,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.events import AnalyzableArticleCreated
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.collection.sources.registry import completion_policy_for
from app.models.outbox_event import OutboxEvent

__all__ = [
    "ArticleCompletionConsumer",
    "CompletionSucceeded",
    "RetryCompletion",
    "NoRetryCompletion",
]


class ArticleCompletionConsumer:
    """DB接続をHTTP待機へ持ち越さず、補完と結果確定を接続する。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory
        self._failure_handler = ArticleCompletionConsumerFailureHandler(session_factory)

    async def consume(
        self, incomplete_article_id: int
    ) -> CompletionSucceeded | RetryCompletion | NoRetryCompletion:
        incomplete: RecordedIncompleteArticle | None = None
        try:
            async with self._session_factory() as session:
                repository = ArticleCompletionConsumerRepository(session)
                incomplete = await repository.load(incomplete_article_id)
                if incomplete is None:
                    return NoRetryCompletion(cause="missing")
                if incomplete.status == "closed":
                    return NoRetryCompletion(cause="closed")
                if await repository.has_completed_article(incomplete.source_url):
                    if not await repository.delete_nonclosed(incomplete_article_id):
                        return NoRetryCompletion(cause="superseded")
                    await session.commit()
                    return NoRetryCompletion(cause="url_conflict")

            observed = ObservedArticle.try_build(
                observed_article=incomplete.observed_article,
                source_name=incomplete.source_name,
                source_url=incomplete.source_url,
            )
            completion_policy = completion_policy_for(observed.source_name)
            raw_response = await fetch_article_response(
                incomplete.source_url.as_web_url()
            )
            scraped_content = extract_html_content(raw_response)
            analyzable_article = complete_with_html(
                observed,
                completion_policy,
                scraped_content,
                source_id=incomplete.source_id,
                source_url=incomplete.source_url,
            )
            return await self._commit_completed(incomplete, analyzable_article)
        except Exception as exc:
            return await self._failure_handler.handle(
                exc,
                incomplete_article_id=incomplete_article_id,
                incomplete=incomplete,
            )

    async def _commit_completed(
        self,
        incomplete: RecordedIncompleteArticle,
        analyzable_article: AnalyzableArticle,
    ) -> CompletionSucceeded | NoRetryCompletion:
        async with self._session_factory() as session:
            deleted = await ArticleCompletionConsumerRepository(
                session
            ).delete_nonclosed(incomplete.incomplete_article_id)
            if not deleted:
                return NoRetryCompletion(cause="superseded")
            article_id = await AnalyzableArticleRepository(session).save(
                analyzable_article
            )
            if article_id is None:
                await session.commit()
                return NoRetryCompletion(cause="url_conflict")

            await ArticleCompletionAuditRepository(session).append_consumer_succeeded(
                incomplete_article_id=incomplete.incomplete_article_id,
                source_id=incomplete.source_id,
                analyzable_article_id=article_id,
                article=analyzable_article,
            )
            event = AnalyzableArticleCreated(analyzable_article_id=article_id)
            session.add(
                OutboxEvent(
                    event_type=event.EVENT_TYPE,
                    schema_version=event.SCHEMA_VERSION,
                    payload=event.model_dump(mode="json"),
                )
            )
            await session.commit()
        record_completion_processing_outcome("succeeded")
        return CompletionSucceeded(analyzable_article_id=article_id)
