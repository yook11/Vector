"""補完の待機指示を、受信したSQSメッセージの可視性変更へ接続する。"""

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

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

# SQSの可視性は受信から12時間までのため、Lambdaの実行時間を残して11時間で切る。
MAX_VISIBILITY_SECONDS = 39_600


@dataclass(frozen=True, slots=True)
class RedeliveryMessage:
    """再配信させるメッセージで、retry_atがなければキューの通常の可視性で再配信される。"""

    message: ReceivedMessage
    retry_at: RetryAt | None = None


@dataclass(frozen=True, slots=True)
class VisibilityTimeout:
    """再試行時刻までの残り時間に配送上の上限を適用した、SQSへ可視性変更を要求する整数秒。"""

    seconds: int
    capped: bool

    @classmethod
    def until(cls, retry_at: RetryAt, now: datetime) -> Self | None:
        """指定時刻から残り秒数を切り上げ、再試行時刻を迎えていれば可視性を変更しないためNoneを返す。"""
        remaining = retry_at.remaining(now)
        if remaining == timedelta():
            return None
        seconds = math.ceil(remaining.total_seconds())
        return cls(
            seconds=min(seconds, MAX_VISIBILITY_SECONDS),
            capped=seconds > MAX_VISIBILITY_SECONDS,
        )


async def apply_visibility_timeouts(
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
        visibility_timeout = VisibilityTimeout.until(retry_at, now())
        if visibility_timeout is None:
            recorder.record_retry_at_passed(retry_at, message_id=message.message_id)
            continue
        if visibility_change_start_deadline.has_passed():
            for unstarted_message, unstarted_retry_at in waits[wait_position:]:
                recorder.record_visibility_change_unstarted(
                    unstarted_retry_at, message_id=unstarted_message.message_id
                )
            break

        try:
            receipt_handle = message.receipt_handle_text()
        except ReceivedMessageInvalidError as exc:
            recorder.record_receipt_handle_invalid(
                retry_at, exc, message_id=message.message_id
            )
            continue

        try:
            await _change_visibility(
                sqs_client,
                queue_url=queue_url,
                receipt_handle=receipt_handle,
                seconds=visibility_timeout.seconds,
            )
        except Exception as exc:
            recorder.record_visibility_change_failed(
                retry_at, visibility_timeout, exc, message_id=message.message_id
            )
        else:
            recorder.record_visibility_changed(
                retry_at, visibility_timeout, message_id=message.message_id
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
