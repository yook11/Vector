"""呼び出し単位でEmbeddingの資源とConsumerを組み立て、終了後に応答する。"""

import asyncio
from contextlib import AsyncExitStack

import structlog

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.lambda_handlers.embedding.failure_handler import EmbeddingLambdaFailureHandler
from app.lambda_handlers.embedding.resources import open_embedding_resources
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.embedding.sqs_batch_handler import (
    SqsBatchResponse,
    process_embedding_messages,
)

logger = structlog.get_logger(__name__)


async def _run_embedding(
    event: object, settings: EmbeddingConsumerSettings
) -> SqsBatchResponse:
    """呼び出し内で資源を共有し、終了してから処理結果を返す。"""
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
            EmbeddingLambdaFailureHandler(logger).handle_initialization_failure(
                stage, exc
            )
            raise
        return await process_embedding_messages(event, consumer=consumer)


def handler(event: object, context: object) -> SqsBatchResponse:
    """今回の初期化・メッセージ処理・資源終了をひとつの非同期実行にまとめる。"""
    try:
        settings = EmbeddingConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        EmbeddingLambdaFailureHandler(logger).handle_initialization_failure(
            "settings", exc
        )
        raise
    return asyncio.run(_run_embedding(event, settings))
