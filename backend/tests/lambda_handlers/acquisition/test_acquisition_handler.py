"""取得Lambda入口に固有の失敗応答と診断境界を確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.collection.article_acquisition.consumer import AcquisitionResult
from app.collection.article_acquisition.consumer_failure_classification import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.lambda_handlers.acquisition import handler as entrypoint
from tests.lambda_handlers.acquisition.test_message import message


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=AcquisitionResult("acquired"))
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
    assert "private-failure-detail" not in repr(runtime.log.mock_calls)
