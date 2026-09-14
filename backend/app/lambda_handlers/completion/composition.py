"""記事取得工程の共通資源へ補完Consumerと配送クライアントを配線する。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import structlog
from botocore.config import Config
from botocore.session import Session
from sqlalchemy.ext.asyncio import AsyncEngine

from app.collection.article_completion.consumer import ArticleCompletionConsumer
from app.db.engine import create_article_fetch_engine
from app.lambda_handlers.article_fetch_lifecycle import (
    ArticleFetchLifecycleRecorder,
    IamPasswordProvider,
    SessionFactory,
    open_article_fetch_consumer,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings

logger = structlog.get_logger(__name__)


class SqsMessageVisibilityClient(Protocol):
    """配送処理が必要とするメッセージの可視性変更だけを表す。"""

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class CompletionResources:
    """配送処理が借用する補完ConsumerとSQSクライアント。"""

    consumer: ArticleCompletionConsumer
    sqs_client: SqsMessageVisibilityClient


@asynccontextmanager
async def open_completion_resources(
    settings: CompletionConsumerSettings,
) -> AsyncIterator[CompletionResources]:
    """SQSの寿命を配送側で管理し、補完Consumerには持ち込まない。"""
    recorder = ArticleFetchLifecycleRecorder(logger, operation="completion")

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
        failure_recorder=recorder,
    ) as consumer:
        try:
            sqs = Session().create_client(
                "sqs",
                region_name=settings.aws_region,
                config=Config(
                    proxies={},
                    ignore_configured_endpoint_urls=True,
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"mode": "standard", "total_max_attempts": 1},
                ),
            )
        except Exception as exc:
            recorder.record_initialization_failure("sqs", exc)
            raise
        try:
            yield CompletionResources(consumer=consumer, sqs_client=sqs)
        finally:
            try:
                sqs.close()
            except Exception as exc:
                recorder.record_cleanup_failure("sqs", exc)
