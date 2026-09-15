"""記事取得Consumerへ呼び出し専用のDBとHTTP設定を配線する。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.collection.article_acquisition.consumer import ArticleAcquisitionConsumer
from app.collection.article_acquisition.reader.crossref_reader import CrossrefReader
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.db.engine import create_article_fetch_engine
from app.lambda_handlers.acquisition.settings import AcquisitionConsumerSettings
from app.lambda_handlers.article_fetch_lifecycle import (
    ArticleFetchLifecycleRecorder,
    IamPasswordProvider,
    SessionFactory,
    open_article_fetch_consumer,
)


@asynccontextmanager
async def open_acquisition_consumer(
    settings: AcquisitionConsumerSettings,
) -> AsyncIterator[ArticleAcquisitionConsumer]:
    def create_engine(*, password_provider: IamPasswordProvider) -> AsyncEngine:
        return create_article_fetch_engine(
            settings,
            application_name="vector-acquisition-consumer",
            password_provider=password_provider,
        )

    def build_consumer(
        *, session_factory: SessionFactory
    ) -> ArticleAcquisitionConsumer:
        return ArticleAcquisitionConsumer(
            session_factory,
            lambda: ReaderTools(
                crossref=CrossrefReader(
                    contact_email=str(settings.crossref_contact_email)
                )
            ),
        )

    async with open_article_fetch_consumer(
        aws_region=settings.aws_region,
        database_url=settings.database_url,
        create_engine=create_engine,
        build_consumer=build_consumer,
        failure_recorder=ArticleFetchLifecycleRecorder(
            structlog.get_logger(__name__), operation="acquisition"
        ),
    ) as consumer:
        yield consumer
