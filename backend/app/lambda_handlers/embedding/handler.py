"""Embedding Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from contextlib import AsyncExitStack
from typing import TypedDict

import structlog

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.lambda_handlers.embedding.event import (
    EmbeddingEventInvalidError,
    parse_assessed_in_scope_event,
)
from app.lambda_handlers.embedding.failure_recorder import (
    EmbeddingLambdaFailureRecorder,
)
from app.lambda_handlers.embedding.resources import open_embedding_resources
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.records import SqsRecordBatch

logger = structlog.get_logger(__name__)


class SqsBatchItemIdentifier(TypedDict):
    itemIdentifier: str


class SqsBatchFailureResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemIdentifier]


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """設定を読んで処理を実行し、失敗したメッセージをAWSへ報告する。"""
    try:
        settings = EmbeddingConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        EmbeddingLambdaFailureRecorder(logger).record_initialization_failure(
            "settings", exc
        )
        raise
    failed_items = asyncio.run(_run_embedding(lambda_event, settings))
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_embedding(
    lambda_event: object, settings: EmbeddingConsumerSettings
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードを処理し、失敗した項目の識別子を返す。"""
    failure_recorder = EmbeddingLambdaFailureRecorder(logger)
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
            failure_recorder.record_initialization_failure(stage, exc)
            raise

        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            failure_recorder.record_invalid_sqs_input(exc)
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record in record_batch.records:
            try:
                message_body = record.body_text()
            except SqsInputError as exc:
                failure_recorder.record_invalid_body(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                assessed_event = parse_assessed_in_scope_event(message_body)
            except EmbeddingEventInvalidError as exc:
                failure_recorder.record_invalid_event(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except Exception as exc:
                failure_recorder.record_message_failure(
                    exc, message_id=record.message_id
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                completion = await consumer.consume(assessed_event.payload)
            except Exception as exc:
                failure_recorder.record_message_failure(
                    exc, message_id=record.message_id, assessed_event=assessed_event
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
            else:
                _log_completion(
                    message_id=record.message_id,
                    event_id=str(assessed_event.event_id),
                    analyzed_article_id=assessed_event.payload.analyzed_article_id,
                    reason=completion.value,
                )
        return failed_items


def _log_completion(**fields: object) -> None:
    try:
        logger.info("embedding_message_completed", **fields)
    except Exception:  # noqa: S110
        # 診断出力の通常障害でメッセージの結果を変えない。
        pass
