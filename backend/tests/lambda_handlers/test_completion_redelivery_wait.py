"""補完の再配信待機をSQSの可視性秒数へ変換し、SQS操作へ接続する条件を、SQSクライアントをモックにし実ロガーの出力で確認する。"""

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.aws.lambda_.sqs_input import parse_sqs_lambda_input
from app.collection.retry_at import RetryAt
from app.lambda_handlers.completion.message_recorder import CompletionMessageRecorder
from app.lambda_handlers.completion.redelivery_wait import (
    RedeliveryMessage,
    VisibilityTimeout,
    apply_visibility_timeouts,
)
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


@pytest.fixture
def read_logs(capsys) -> Callable[[], list[dict[str, object]]]:
    """出力されたログを、実行ごとに変わる時刻と発生位置を除いて返す。"""

    def _read() -> list[dict[str, object]]:
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        for log in logs:
            del log["timestamp"]
            log.pop("frames", None)
        return logs

    return _read


@pytest.fixture
def delivery():
    state = SimpleNamespace(
        now=datetime(2026, 9, 14, tzinfo=UTC),
        context=SimpleNamespace(get_remaining_time_in_millis=Mock(return_value=20_000)),
        sqs=Mock(),
    )
    state.first, state.second = parse_sqs_lambda_input(
        {
            "Records": [
                {
                    "messageId": "first",
                    "body": "{}",
                    "receiptHandle": " private-first ",
                },
                {
                    "messageId": "second",
                    "body": "{}",
                    "receiptHandle": "private-second",
                },
            ]
        }
    )
    return state


async def apply(state, waits):
    await apply_visibility_timeouts(
        waits,
        sqs_client=state.sqs,
        queue_url="configured-queue",
        context=state.context,
        now=lambda: state.now,
        recorder=CompletionMessageRecorder(
            create_policy_json_logger("article_fetch", EXTERNAL_CONTENT_LOG_RULES)
        ),
    )


# 39,600秒は配送上の上限の11時間。
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, None),
        (0.000001, VisibilityTimeout(seconds=1, capped=False)),
        (39_600, VisibilityTimeout(seconds=39_600, capped=False)),
        (39_600.1, VisibilityTimeout(seconds=39_600, capped=True)),
        (86_400, VisibilityTimeout(seconds=39_600, capped=True)),
    ],
)
def test_visibility_timeout_rounds_up_and_caps_remaining_time(seconds, expected):
    """retry_atまでの残り時間を整数秒へ切り上げて上限で切り、期限を迎えていればNoneにする。"""
    now = datetime(2026, 9, 14, tzinfo=UTC)
    retry = RetryAt(now + timedelta(seconds=seconds))
    assert VisibilityTimeout.until(retry, now) == expected


@pytest.mark.asyncio
async def test_capped_visibility_keeps_original_retry_time(delivery, read_logs):
    """上限で切った秒数をSQSへ要求し、記録する待機時刻は元のretry_atのまま残す。"""
    retry = RetryAt(delivery.now + timedelta(seconds=86_400))
    await apply(delivery, [RedeliveryMessage(delivery.first, retry)])
    delivery.sqs.change_message_visibility.assert_called_once_with(
        QueueUrl="configured-queue",
        ReceiptHandle=" private-first ",
        VisibilityTimeout=39_600,
    )
    assert read_logs() == [
        {
            "event": "completion_redelivery_wait",
            "level": "info",
            "log_policy": "external_content_fetch",
            "message_id": "first",
            "retry_at": "2026-09-15T00:00:00+00:00",
            "result": "applied",
            "visibility_timeout_seconds": 39_600,
            "capped": True,
        }
    ]


@pytest.mark.asyncio
async def test_expired_wait_is_not_sent(delivery, read_logs):
    """設定時点で期限を迎えた待機をSQSへ送らない。"""
    await apply(delivery, [RedeliveryMessage(delivery.first, RetryAt(delivery.now))])
    delivery.sqs.change_message_visibility.assert_not_called()
    assert read_logs() == [
        {
            "event": "completion_redelivery_wait",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "message_id": "first",
            "retry_at": "2026-09-14T00:00:00+00:00",
            "result": "expired",
        }
    ]


@pytest.mark.asyncio
async def test_message_without_retry_time_keeps_queue_visibility(delivery):
    """待機時刻のない再配信メッセージは可視性を変えず、待機時刻のあるメッセージだけを設定する。"""
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery,
        [RedeliveryMessage(delivery.first), RedeliveryMessage(delivery.second, retry)],
    )
    assert delivery.sqs.change_message_visibility.call_args_list == [
        call(
            QueueUrl="configured-queue",
            ReceiptHandle="private-second",
            VisibilityTimeout=120,
        )
    ]


@pytest.mark.asyncio
async def test_wait_is_recalculated_after_previous_operation(delivery):
    """直前の通信時間を次の待機秒数へ反映する。"""
    retry = RetryAt(delivery.now + timedelta(seconds=120))

    def advance(**kwargs):
        delivery.now += timedelta(seconds=30)

    delivery.sqs.change_message_visibility.side_effect = advance
    await apply(
        delivery,
        [
            RedeliveryMessage(delivery.first, retry),
            RedeliveryMessage(delivery.second, retry),
        ],
    )
    assert [
        c.kwargs["VisibilityTimeout"]
        for c in delivery.sqs.change_message_visibility.call_args_list
    ] == [120, 90]


@pytest.mark.asyncio
async def test_failed_wait_does_not_prevent_next_wait(delivery, read_logs):
    """通常の設定失敗の後も次の待機設定へ進み、失敗は設定しようとした秒数と原因を記録する。"""
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    delivery.sqs.change_message_visibility.side_effect = [
        RuntimeError("SQS request failed"),
        {},
    ]
    await apply(
        delivery,
        [
            RedeliveryMessage(delivery.first, retry),
            RedeliveryMessage(delivery.second, retry),
        ],
    )
    assert read_logs() == [
        {
            "event": "completion_redelivery_wait",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "message_id": "first",
            "retry_at": "2026-09-14T00:02:00+00:00",
            "result": "failed",
            "visibility_timeout_seconds": 120,
            "capped": False,
            "error_class": "builtins.RuntimeError",
            "error_message": "SQS request failed",
        },
        {
            "event": "completion_redelivery_wait",
            "level": "info",
            "log_policy": "external_content_fetch",
            "message_id": "second",
            "retry_at": "2026-09-14T00:02:00+00:00",
            "result": "applied",
            "visibility_timeout_seconds": 120,
            "capped": False,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("millis,expected", [(20_000, 1), (19_999, 0)])
async def test_visibility_start_boundary(delivery, millis, expected):
    """残り20秒以上のときだけ待機設定を開始する。"""
    delivery.context.get_remaining_time_in_millis.return_value = millis
    await apply(
        delivery,
        [
            RedeliveryMessage(
                delivery.first, RetryAt(delivery.now + timedelta(seconds=120))
            )
        ],
    )
    assert delivery.sqs.change_message_visibility.call_count == expected


@pytest.mark.asyncio
async def test_insufficient_time_stops_remaining_waits(delivery, read_logs):
    """途中の時間不足は残りの待機設定を打ち切り記録する。"""
    delivery.context.get_remaining_time_in_millis.side_effect = [20_000, 19_999]
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery,
        [
            RedeliveryMessage(delivery.first, retry),
            RedeliveryMessage(delivery.second, retry),
        ],
    )
    assert delivery.sqs.change_message_visibility.call_count == 1
    assert read_logs()[-1] == {
        "event": "completion_redelivery_wait",
        "level": "warning",
        "log_policy": "external_content_fetch",
        "message_id": "second",
        "retry_at": "2026-09-14T00:02:00+00:00",
        "result": "insufficient_time",
    }


@pytest.mark.asyncio
async def test_logging_failure_does_not_stop_waits(delivery, monkeypatch):
    """診断障害が後続の待機設定を妨げない。"""

    class BrokenOutput:
        def write(self, text):
            raise OSError("private-log")

        def flush(self):
            raise OSError("private-log")

    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    retry = RetryAt(delivery.now + timedelta(seconds=120))
    await apply(
        delivery,
        [
            RedeliveryMessage(delivery.first, retry),
            RedeliveryMessage(delivery.second, retry),
        ],
    )
    assert delivery.sqs.change_message_visibility.call_count == 2
