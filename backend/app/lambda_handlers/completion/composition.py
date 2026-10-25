"""記事取得工程の共通資源へ補完Consumerと配送クライアントを配線する。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from botocore.session import Session
from sqlalchemy.ext.asyncio import AsyncEngine

from app.aws.sqs.visibility import (
    SqsMessageVisibilityClient,
    create_sqs_visibility_client,
)
from app.collection.article_completion.consumer import ArticleCompletionConsumer
from app.db.engine import create_article_fetch_engine
from app.lambda_handlers.article_fetch_lifecycle import (
    ArticleFetchLifecycleRecorder,
    IamPasswordProvider,
    SessionFactory,
    open_article_fetch_consumer,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings


@dataclass(frozen=True, slots=True)
class CompletionResources:
    """配送処理が借用する補完ConsumerとSQSクライアント。"""

    consumer: ArticleCompletionConsumer
    sqs_client: SqsMessageVisibilityClient


@asynccontextmanager
async def open_completion_resources(
    settings: CompletionConsumerSettings,
    *,
    failure_recorder: ArticleFetchLifecycleRecorder,
) -> AsyncIterator[CompletionResources]:
    """SQSの寿命を配送側で管理し、補完Consumerには持ち込まない。"""

    def create_engine(*, password_provider: IamPasswordProvider) -> AsyncEngine:
        return create_article_fetch_engine(
            settings,
            application_name="vector-completion-consumer",
            password_provider=password_provider,
        )

    def build_consumer(*, session_factory: SessionFactory) -> ArticleCompletionConsumer:
        return ArticleCompletionConsumer(session_factory)

    async with open_article_fetch_consumer(
        aws_region=settings.aws_region,
        database_url=settings.database_url,
        create_engine=create_engine,
        build_consumer=build_consumer,
        failure_recorder=failure_recorder,
    ) as consumer:
        try:
            sqs = create_sqs_visibility_client(
                session=Session(), region=settings.aws_region
            )
        except Exception as exc:
            failure_recorder.record_initialization_failure("sqs", exc)
            raise
        try:
            yield CompletionResources(consumer=consumer, sqs_client=sqs)
        finally:
            try:
                sqs.close()
            except Exception as exc:
                failure_recorder.record_cleanup_failure("sqs", exc)
