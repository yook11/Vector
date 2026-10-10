"""補完の待機指示を、受信したSQSメッセージの可視性変更へ接続する。"""

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.collection.retry_at import RetryAt
from app.lambda_handlers.completion.composition import SqsMessageVisibilityClient
from app.lambda_handlers.completion.failure_recorder import (
    CompletionLambdaFailureRecorder,
)
from app.lambda_handlers.completion.processing_start_deadline import (
    LambdaContext,
    ProcessingStartDeadline,
)
from app.lambda_handlers.sqs.received_message import (
    ReceivedMessage,
    ReceivedMessageInvalidError,
)

MAX_VISIBILITY_SECONDS = 39_600


@dataclass(frozen=True, slots=True)
class RedeliveryMessage:
    """再配信させるメッセージで、retry_atがなければキューの通常の可視性で再配信される。"""

    message: ReceivedMessage
    retry_at: RetryAt | None = None


async def apply_redelivery_waits(
    redelivery_messages: Sequence[RedeliveryMessage],
    *,
    sqs_client: SqsMessageVisibilityClient,
    queue_url: str,
    context: LambdaContext,
    now: Callable[[], datetime],
    recorder: CompletionLambdaFailureRecorder,
) -> None:
    """retry_atのあるメッセージだけを記事処理後に逐次設定し、設定結果によって配送応答を変更しない。"""
    # 応答を間に合わせるため、待機を設定してよいのはLambdaの制限時間の20秒前までとする。
    visibility_change_start_deadline = ProcessingStartDeadline(
        context, before_lambda_limit=timedelta(seconds=20)
    )
    waits = [
        (redelivery_message.message, redelivery_message.retry_at)
        for redelivery_message in redelivery_messages
        if redelivery_message.retry_at is not None
    ]
    for wait_position, (message, retry_at) in enumerate(waits):
        remaining = retry_at.remaining(now())
        if remaining == timedelta():
            recorder.record_redelivery_wait(
                retry_at, message_id=message.message_id, result="expired"
            )
            continue
        if visibility_change_start_deadline.has_passed():
            for unstarted_message, unstarted_retry_at in waits[wait_position:]:
                recorder.record_redelivery_wait(
                    unstarted_retry_at,
                    message_id=unstarted_message.message_id,
                    result="insufficient_time",
                )
            break
        try:
            receipt_handle = message.receipt_handle_text()
        except ReceivedMessageInvalidError as exc:
            recorder.record_redelivery_wait(
                retry_at,
                message_id=message.message_id,
                result="invalid_receipt_handle",
                reason=exc.reason.value,
            )
            continue
        seconds = math.ceil(remaining.total_seconds())
        requested_seconds = min(seconds, MAX_VISIBILITY_SECONDS)
        capped = seconds > MAX_VISIBILITY_SECONDS
        try:
            await _change_visibility(
                sqs_client,
                queue_url=queue_url,
                receipt_handle=receipt_handle,
                seconds=requested_seconds,
            )
        except Exception as exc:
            recorder.record_redelivery_wait(
                retry_at,
                message_id=message.message_id,
                result="failed",
                requested_seconds=requested_seconds,
                capped=capped,
                error=exc,
            )
        else:
            recorder.record_redelivery_wait(
                retry_at,
                message_id=message.message_id,
                result="applied",
                requested_seconds=requested_seconds,
                applied_seconds=requested_seconds,
                capped=capped,
            )


async def _change_visibility(
    client: SqsMessageVisibilityClient,
    *,
    queue_url: str,
    receipt_handle: str,
    seconds: int,
) -> None:
    operation = asyncio.create_task(
        asyncio.to_thread(
            client.change_message_visibility,
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )
    )
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        # 実行中のSDK通信は中断できないため、重なるキャンセルからも回収を守る。
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled():
            operation.exception()
        raise
