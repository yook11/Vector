"""Consumerとロガーをモックにし、取得Lambda入口の配送判断と診断境界を確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.collection.article_acquisition.consumer_result import (
    AcquisitionSucceeded,
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.lambda_handlers.acquisition import handler as entrypoint
from tests.lambda_handlers.acquisition.test_message import message


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=AcquisitionSucceeded(0))
        ),
        log=Mock(),
    )

    @asynccontextmanager
    async def open_consumer(settings):
        yield state.consumer

    monkeypatch.setattr(entrypoint, "open_acquisition_consumer", open_consumer)
    monkeypatch.setattr(entrypoint, "AcquisitionConsumerSettings", Mock())
    monkeypatch.setattr(entrypoint, "setup_lambda_logging", Mock())
    monkeypatch.setattr(entrypoint, "logger", state.log)
    return state


def test_id_mismatch_fails_message_without_exposing_body(runtime):
    """ID不一致の本文は取得せず、本文を診断へ出さずに再配送を要求する。"""
    response = entrypoint.handler(
        {
            "Records": [
                {
                    "messageId": "invalid",
                    "body": json.dumps(message(request_id="private-id")),
                }
            ]
        },
        None,
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    runtime.consumer.consume.assert_not_awaited()
    assert runtime.log.info.call_args.kwargs["code"] == "invalid_request"
    assert runtime.log.info.call_args.kwargs["operation"] == "validate_event"
    assert "private-id" not in repr(runtime.log.mock_calls)


def test_missing_message_id_fails_invocation_before_consumption(runtime):
    """失敗応答の識別子を確定できなければ部分応答を返さない。"""
    with pytest.raises(RuntimeError, match="acquisition_initialization_failed"):
        entrypoint.handler({"Records": [{"body": json.dumps(message())}]}, None)
    runtime.consumer.consume.assert_not_awaited()


def test_cancelled_acquisition_is_not_a_message_failure(runtime):
    """キャンセルは再配送一覧への変換で握りつぶさない。"""
    runtime.consumer.consume.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        entrypoint.handler(
            {"Records": [{"messageId": "cancelled", "body": json.dumps(message())}]},
            None,
        )


def test_diagnostic_failure_preserves_completed_result(runtime):
    """診断の出力障害で確定した取得結果を失敗に変えない。"""
    runtime.log.info.side_effect = RuntimeError("private-log-detail")
    assert entrypoint.handler(
        {"Records": [{"messageId": "done", "body": json.dumps(message())}]}, None
    ) == {"batchItemFailures": []}


@pytest.mark.parametrize(
    ("failure_type", "expected_failures", "disposition"),
    [
        (RetryAcquisition, [{"itemIdentifier": "failed"}], "batch_item_failure"),
        (NoRetryAcquisition, [], "completed"),
    ],
)
def test_acquisition_failure_is_redelivered_only_when_decided_to_retry(
    runtime, failure_type, expected_failures, disposition
):
    """取得の失敗は再配信と判断したときだけ返し、それ以外は受信完了にする。"""
    runtime.consumer.consume.return_value = failure_type(
        RuntimeError("private-failure-detail")
    )
    response = entrypoint.handler(
        {"Records": [{"messageId": "failed", "body": json.dumps(message())}]}, None
    )
    assert response == {"batchItemFailures": expected_failures}
    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "failed"
    assert fields["code"] == "processing_failed"
    assert fields["error_class"] == "builtins.RuntimeError"
    assert fields["message_disposition"] == disposition
    assert fields["duration_ms"] >= 0
    assert "duration_seconds" not in fields
    assert "operation" not in fields
    assert "private-failure-detail" not in repr(runtime.log.mock_calls)


def test_consumer_exception_is_logged_as_call_failure_and_redelivered(
    runtime, monkeypatch
):
    """結果を受け取れなかった例外は自由文を出さず、呼び出し失敗として記録し再配信する。"""
    runtime.consumer.consume.side_effect = RuntimeError("private-consume-detail")
    monkeypatch.setattr(entrypoint, "perf_counter", lambda: 100.0)
    monkeypatch.setattr("app.shared.time.perf_counter", lambda: 100.125)

    response = entrypoint.handler(
        {"Records": [{"messageId": "raised", "body": json.dumps(message())}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "raised"}]}
    runtime.log.info.assert_called_once()
    assert runtime.log.info.call_args.args == ("acquisition_message_processed",)
    assert runtime.log.info.call_args.kwargs == {
        "message_id": "raised",
        "request_id": "high/2026-09-15T00:00:00Z/1",
        "source_id": 1,
        "result": "failed",
        "code": "processing_failed",
        "operation": "consume",
        "error_class": "builtins.RuntimeError",
        "message_disposition": "batch_item_failure",
        "duration_ms": 125.0,
    }


def test_read_failure_is_logged_before_following_message_is_consumed(
    runtime, monkeypatch
):
    """JSONを読めなかったメッセージだけ再配信し、後続の正常な依頼を処理する。"""
    monkeypatch.setattr(entrypoint, "perf_counter", lambda: 100.0)
    monkeypatch.setattr("app.shared.time.perf_counter", lambda: 100.25)
    response = entrypoint.handler(
        {
            "Records": [
                {"messageId": "invalid-json", "body": "private-invalid-json"},
                {"messageId": "following", "body": json.dumps(message())},
            ]
        },
        None,
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid-json"}]}
    runtime.consumer.consume.assert_awaited_once()
    records = [call.kwargs for call in runtime.log.info.call_args_list]
    assert records[0]["operation"] == "parse_message"
    assert records[0]["code"] == "invalid_request"
    assert records[0]["duration_ms"] == 250.0
    assert "duration_seconds" not in records[0]
    assert records[1]["message_id"] == "following"
    assert records[1]["result"] == "acquired"
    assert records[1]["duration_ms"] == 250.0
    assert "duration_seconds" not in records[1]
    assert "private-invalid-json" not in repr(runtime.log.mock_calls)


def test_mixed_batch_redelivers_only_retry_results_and_call_failures(runtime):
    """混在バッチで呼び出し例外の後も処理を続け、再配信対象のIDだけを入力順で返す。"""
    runtime.consumer.consume.side_effect = [
        AcquisitionSucceeded(1),
        RetryAcquisition(RuntimeError("retry")),
        RuntimeError("consume-failed"),
        NoRetryAcquisition(cause="inactive"),
        NoRetryAcquisition(cause=RuntimeError("no-retry")),
        AcquisitionSucceeded(2),
    ]
    message_ids = ["first", "retry", "raised", "inactive", "no-retry", "last"]

    response = entrypoint.handler(
        {
            "Records": [
                {"messageId": message_id, "body": json.dumps(message())}
                for message_id in message_ids
            ]
        },
        None,
    )

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "retry"},
            {"itemIdentifier": "raised"},
        ]
    }
    assert runtime.consumer.consume.await_count == 6
    records = [call.kwargs for call in runtime.log.info.call_args_list]
    assert [record["message_id"] for record in records] == message_ids
    assert records[-1]["created_count"] == 2


def test_diagnostic_failure_preserves_retry_result(runtime):
    """再配信と決めた結果はログ出力が失敗しても再配信対象から外さない。"""
    runtime.consumer.consume.return_value = RetryAcquisition(RuntimeError("retry"))
    runtime.log.info.side_effect = RuntimeError("private-log-detail")

    response = entrypoint.handler(
        {"Records": [{"messageId": "retry", "body": json.dumps(message())}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
