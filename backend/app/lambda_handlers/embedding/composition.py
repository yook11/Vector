"""呼び出しの利用範囲に合わせてEmbeddingConsumerを組み立てる。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import structlog

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.lambda_handlers.embedding.failure_recorder import (
    EmbeddingLambdaFailureRecorder,
)
from app.lambda_handlers.embedding.resources import open_embedding_resources
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def open_embedding_consumer(
    settings: EmbeddingConsumerSettings,
) -> AsyncIterator[EmbeddingConsumer]:
    """準備済みConsumerを借用させ、利用終了後に所有資源を解放する。"""
    async with AsyncExitStack() as stack:
        stage = "resources"
        try:
            resources = await stack.enter_async_context(
                open_embedding_resources(settings)
            )
            stage = "gemini_client"
            client = await stack.enter_async_context(
                open_gemini_client(
                    api_key=resources.gemini_api_key,
                    settings=GeminiConnectionSettings(),
                )
            )
            stage = "consumer"
            consumer = EmbeddingConsumer(
                resources.session_factory, GeminiEmbedder(client=client)
            )
        except Exception as exc:
            EmbeddingLambdaFailureRecorder(logger).record_initialization_failure(
                stage, exc
            )
            raise
        yield consumer
