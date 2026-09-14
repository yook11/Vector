"""補完の再配信待機をSQS操作へ接続する条件を確認する。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.collection.article_completion.retry_at import RetryAt
from app.lambda_handlers.completion.failure_recorder import (
    CompletionLambdaFailureRecorder,
)
from app.lambda_handlers.completion.redelivery_wait import (
    RedeliveryWait,
    apply_redelivery_waits,
)
from app.lambda_handlers.sqs.records import SqsRecordBatch


@pytest.fixture
def delivery():
    state = SimpleNamespace(
        now=datetime(2026, 9, 14, tzinfo=UTC),
        context=SimpleNamespace(get_remaining_time_in_millis=Mock(return_value=20_000)),
        sqs=Mock(),
        log=Mock(),
    )
    state.records = SqsRecordBatch.from_lambda_event(
        {
            "Records": [
                {"messageId": "first", "receiptHandle": " private-first "},
                {"messageId": "second", "receiptHandle": "private-second"},
            ]
        }
    ).records
    return state


async def apply(state, waits):
    await apply_redelivery_waits(
        waits,
        records=state.records,
        sqs_client=state.sqs,
        queue_url="configured-queue",
        context=state.context,
        now=lambda: state.now,
        recorder=CompletionLambdaFailureRecorder(state.log),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds,expected,capped",
    [
        (0.000001, 1, False),
        (39_600, 39_600, False),
        (39_600.1, 39_600, True),
        (86_400, 39_600, True),
    ],
)
async def test_visibility_rounds_up_and_caps_without_changing_retry_time(
    delivery, seconds, expected, capped
):
    """SQS秒数だけを切り上げ・制限し、元の時刻を保持する。"""
    retry = RetryAt(delivery.now + timedelta(seconds=seconds))
    await apply(delivery, [RedeliveryWait("first", retry)])
    delivery.sqs.change_message_visibility.assert_called_once_with(
        QueueUrl="configured-queue",
        ReceiptHandle=" private-first ",
        VisibilityTimeout=expected,
    )
    fields = delivery.log.warning.call_args.kwargs
    assert fields["retry_at"] == retry.value.isoformat()
    assert fields["requested_seconds"] == fields["applied_seconds"] == expected
    assert fields["capped"] is capped


@pytest.mark.asyncio
async def test_expired_wait_is_not_sent(delivery):
    """設定時点で期限を迎えた待機をSQSへ送らない。"""
    await apply(delivery, [RedeliveryWait("first", RetryAt(delivery.now))])
    delivery.sqs.change_message_visibility.assert_not_called()
    assert delivery.log.warning.call_args.kwargs["result"] == "expired"


@pytest.mark.asyncio
async def test_wait_is_recalculated_after_previous_operation(delivery):
    """直前の通信時間を次の待機秒数へ反映する。"""
    retry = RetryAt(delivery.now + timedelta(seconds=120))

    def advance(**kwargs):
        delivery.now += timedelta(seconds=30)

    delivery.sqs.change_message_visibility.side_effect = advance
    await apply(
        delivery, [RedeliveryWait("first", retry), RedeliveryWait("second", retry)]
    )
    assert [
        c.kwargs["VisibilityTimeout"]
        for c in delivery.sqs.change_message_visibility.call_args_list
    ] == [120, 90]


@pytest.mark.asyncio
async def test_failed_wait_does_not_prevent_next_wait(delivery):
    """通常の設定失敗の後も次の待機設定へ進む。"""
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    delivery.sqs.change_message_visibility.side_effect = [
        RuntimeError("private-error"),
        {},
    ]
    await apply(
        delivery, [RedeliveryWait("first", retry), RedeliveryWait("second", retry)]
    )
    fields = [c.kwargs for c in delivery.log.warning.call_args_list]
    assert [f["result"] for f in fields] == ["failed", "applied"]
    assert fields[0]["applied_seconds"] is None
    assert fields[0]["requested_seconds"] == 120
    assert "private" not in repr(delivery.log.mock_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("millis,expected", [(20_000, 1), (19_999, 0)])
async def test_visibility_start_boundary(delivery, millis, expected):
    """残り20秒以上のときだけ待機設定を開始する。"""
    delivery.context.get_remaining_time_in_millis.return_value = millis
    await apply(
        delivery,
        [RedeliveryWait("first", RetryAt(delivery.now + timedelta(seconds=120)))],
    )
    assert delivery.sqs.change_message_visibility.call_count == expected


@pytest.mark.asyncio
async def test_insufficient_time_stops_remaining_waits(delivery):
    """途中の時間不足は残りの待機設定を打ち切り記録する。"""
    delivery.context.get_remaining_time_in_millis.side_effect = [20_000, 19_999]
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery, [RedeliveryWait("first", retry), RedeliveryWait("second", retry)]
    )
    assert delivery.sqs.change_message_visibility.call_count == 1
    assert delivery.log.warning.call_args.kwargs["result"] == "insufficient_time"
    assert delivery.log.warning.call_args.kwargs["message_id"] == "second"


@pytest.mark.asyncio
async def test_invalid_receipt_skips_only_its_wait(delivery):
    """receiptHandle不正は当該設定だけを省略する。"""
    delivery.records = SqsRecordBatch.from_lambda_event(
        {
            "Records": [
                {
                    "messageId": "first",
                    "receiptHandle": {"private-key": "private-value"},
                },
                {"messageId": "second", "receiptHandle": "valid"},
            ]
        }
    ).records
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery, [RedeliveryWait("first", retry), RedeliveryWait("second", retry)]
    )
    assert delivery.sqs.change_message_visibility.call_args_list == [
        call(QueueUrl="configured-queue", ReceiptHandle="valid", VisibilityTimeout=120)
    ]
    assert (
        delivery.log.warning.call_args_list[0].kwargs["result"]
        == "invalid_receipt_handle"
    )
    assert "private" not in repr(delivery.log.mock_calls)


@pytest.mark.asyncio
async def test_logging_failure_does_not_stop_waits(delivery):
    """診断障害が後続の待機設定を妨げない。"""
    delivery.log.warning.side_effect = RuntimeError("private-log")
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery, [RedeliveryWait("first", retry), RedeliveryWait("second", retry)]
    )
    assert delivery.sqs.change_message_visibility.call_count == 2
