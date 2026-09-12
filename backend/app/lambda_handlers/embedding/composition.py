"""記事単位AI分析のライフサイクルへEmbeddingの依存を配線する。"""

from contextlib import AbstractAsyncContextManager

import structlog
from google.genai.client import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.db.engine import create_embedding_consumer_engine
from app.lambda_handlers.article_analysis_lifecycle import (
    IamPasswordProvider,
    SessionFactory,
    open_article_analysis_consumer,
)
from app.lambda_handlers.embedding.failure_recorder import (
    EmbeddingLambdaFailureRecorder,
)
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings

logger = structlog.get_logger(__name__)


def open_embedding_consumer(
    settings: EmbeddingConsumerSettings,
) -> AbstractAsyncContextManager[EmbeddingConsumer]:
    """工程別の生成関数を渡し、資源の準備・終了順序を共通側へ委ねる。"""

    def create_engine(*, password_provider: IamPasswordProvider) -> AsyncEngine:
        return create_embedding_consumer_engine(
            settings, password_provider=password_provider
        )

    def open_client(*, api_key: SecretStr) -> AbstractAsyncContextManager[AsyncClient]:
        return open_gemini_client(
            api_key=api_key,
            settings=GeminiConnectionSettings(),
        )

    def build_consumer(
        *, session_factory: SessionFactory, client: AsyncClient
    ) -> EmbeddingConsumer:
        return EmbeddingConsumer(session_factory, GeminiEmbedder(client=client))

    return open_article_analysis_consumer(
        aws_region=settings.aws_region,
        database_url=settings.database_url,
        api_key_parameter_path=settings.gemini_api_key_parameter_path,
        create_engine=create_engine,
        open_client=open_client,
        build_consumer=build_consumer,
        failure_recorder=EmbeddingLambdaFailureRecorder(logger),
    )
