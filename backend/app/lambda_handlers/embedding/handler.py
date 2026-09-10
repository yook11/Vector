"""Embedding Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from contextlib import AsyncExitStack
from typing import TypedDict

import structlog

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.lambda_handlers.embedding.failure_handler import EmbeddingLambdaFailureHandler
from app.lambda_handlers.embedding.record_handler import process_embedding_record
from app.lambda_handlers.embedding.resources import open_embedding_resources
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.records import SqsRecordBatch

logger = structlog.get_logger(__name__)


class SqsBatchItemFailure(TypedDict):
    itemIdentifier: str


class SqsBatchResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemFailure]


def handler(event: object, context: object) -> SqsBatchResponse:
    """設定を読み、今回のLambda実行を開始する。"""
    try:
        settings = EmbeddingConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        EmbeddingLambdaFailureHandler(logger).handle_initialization_failure(
            "settings", exc
        )
        raise
    return asyncio.run(_run_embedding(event, settings))


async def _run_embedding(
    event: object, settings: EmbeddingConsumerSettings
) -> SqsBatchResponse:
    """資源の準備から各レコードの処理、応答と終了までを進める。"""
    failure_handler = EmbeddingLambdaFailureHandler(logger)
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
            failure_handler.handle_initialization_failure(stage, exc)
            raise

        try:
            batch = SqsRecordBatch.from_input(event)
        except SqsInputError as exc:
            failure_handler.handle_invalid_sqs_input(exc)
            raise

        failures: list[SqsBatchItemFailure] = []
        for record in batch.records:
            succeeded = await process_embedding_record(record, consumer=consumer)
            if not succeeded:
                failures.append({"itemIdentifier": record.message_id})
        return {"batchItemFailures": failures}
