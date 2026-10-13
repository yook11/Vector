"""Consumerをモックにし、取得Lambda入口の配送判断と実ロガーの出力を確認する。"""

import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.collection.article_acquisition.consumer_result import (
    AcquisitionSucceeded,
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.http.errors import HttpResponseError
from app.lambda_handlers.acquisition import handler as entrypoint
from tests.lambda_handlers.acquisition.test_message import message


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=AcquisitionSucceeded(0))
        ),
        context=SimpleNamespace(aws_request_id="request-001"),
    )

    @asynccontextmanager
    async def open_consumer(settings, *, failure_recorder):
        yield state.consumer

    monkeypatch.setattr(entrypoint, "open_acquisition_consumer", open_consumer)
    monkeypatch.setattr(
        entrypoint,
        "AcquisitionConsumerSettings",
        Mock(return_value=SimpleNamespace(env="test")),
    )
    # 所要時間を固定し、ログを丸ごと比べられるようにする。
    monkeypatch.setattr(entrypoint, "perf_counter", lambda: 100.0)
    monkeypatch.setattr("app.shared.time.perf_counter", lambda: 100.125)
    return state


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
def broken_output(monkeypatch):
    """ログの書き込みが毎回失敗する標準出力へ差し替える。"""

    class BrokenOutput:
        def write(self, text):
            raise OSError("private-log")

        def flush(self):
            raise OSError("private-log")

    monkeypatch.setattr(sys, "stdout", BrokenOutput())


def test_id_mismatch_fails_message_without_exposing_body(runtime, read_logs):
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
        runtime.context,
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    runtime.consumer.consume.assert_not_awaited()
    assert read_logs()[-1] == {
        "event": "acquisition_message_processing_failed",
        "level": "error",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "acquisition",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "invalid",
        "operation": "validate_event",
        "duration_ms": 125.0,
        "message_disposition": "batch_item_failure",
        "error_class": (
            "app.collection.sources.acquisition_request.AcquisitionRequestInvalidError"
        ),
        "error_message": "invalid_acquisition_request",
    }


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


def test_diagnostic_failure_preserves_completed_result(runtime, broken_output):
    """診断の出力障害で確定した取得結果を失敗に変えない。"""
    assert entrypoint.handler(
        {"Records": [{"messageId": "done", "body": json.dumps(message())}]}, None
    ) == {"batchItemFailures": []}


@pytest.mark.parametrize(
    ("result", "status_code", "expected_failures", "level", "disposition"),
    [
        pytest.param(
            RetryAcquisition(
                HttpResponseError(
                    status_code=503, received_at=datetime(2026, 9, 15, tzinfo=UTC)
                ),
                code="http_response_error",
            ),
            503,
            [{"itemIdentifier": "failed"}],
            "warning",
            "batch_item_failure",
            id="retry",
        ),
        pytest.param(
            NoRetryAcquisition(
                HttpResponseError(
                    status_code=404, received_at=datetime(2026, 9, 15, tzinfo=UTC)
                ),
                code="http_response_error",
                requires_investigation=True,
            ),
            404,
            [],
            "error",
            "completed",
            id="no_retry",
        ),
    ],
)
def test_acquisition_failure_is_redelivered_only_when_decided_to_retry(
    runtime, read_logs, result, status_code, expected_failures, level, disposition
):
    """取得の失敗は再配信と判断したときだけ返し、調査要否のレベルで原因の診断とともに記録する。"""
    runtime.consumer.consume.return_value = result
    response = entrypoint.handler(
        {"Records": [{"messageId": "failed", "body": json.dumps(message())}]},
        runtime.context,
    )
    assert response == {"batchItemFailures": expected_failures}
    assert read_logs()[-1] == {
        "event": "acquisition_message_processing_failed",
        "level": level,
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "acquisition",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "failed",
        "acquisition_request_id": "high/2026-09-15T00:00:00Z/1",
        "source_id": 1,
        "code": "http_response_error",
        "requires_investigation": result.requires_investigation,
        "duration_ms": 125.0,
        "message_disposition": disposition,
        "error_class": "app.http.errors.HttpResponseError",
        "error_message": "HTTPの非成功応答を受け取りました",
        "error_details": {"status_code": status_code},
    }


def test_consumer_exception_is_logged_as_call_failure_and_redelivered(
    runtime, read_logs
):
    """結果を受け取れなかった例外は、呼び出しの失敗として変換した原因とともに記録し再配信する。"""
    runtime.consumer.consume.side_effect = RuntimeError("consume-raised")

    response = entrypoint.handler(
        {"Records": [{"messageId": "raised", "body": json.dumps(message())}]},
        runtime.context,
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "raised"}]}
    assert read_logs()[-1] == {
        "event": "acquisition_message_processing_failed",
        "level": "error",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "acquisition",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "raised",
        "acquisition_request_id": "high/2026-09-15T00:00:00Z/1",
        "source_id": 1,
        "operation": "consume",
        "duration_ms": 125.0,
        "message_disposition": "batch_item_failure",
        "error_class": "builtins.RuntimeError",
        "error_message": "consume-raised",
    }


def test_read_failure_is_logged_before_following_message_is_consumed(
    runtime, read_logs
):
    """JSONを読めなかったメッセージだけ再配信し、後続の正常な依頼を処理する。"""
    response = entrypoint.handler(
        {
            "Records": [
                {"messageId": "invalid-json", "body": "private-invalid-json"},
                {"messageId": "following", "body": json.dumps(message())},
            ]
        },
        runtime.context,
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid-json"}]}
    runtime.consumer.consume.assert_awaited_once()
    terminal_logs = [
        log
        for log in read_logs()
        if log["event"] != "acquisition_message_processing_started"
    ]
    assert terminal_logs == [
        {
            "event": "acquisition_message_processing_failed",
            "level": "error",
            "log_policy": "external_content_fetch",
            "service": "article_fetch",
            "stage": "acquisition",
            "request_id": "request-001",
            "environment": "test",
            "message_id": "invalid-json",
            "operation": "parse_message",
            "duration_ms": 125.0,
            "message_disposition": "batch_item_failure",
            "error_class": (
                "app.lambda_handlers.sqs.received_message.ReceivedMessageInvalidError"
            ),
            "error_message": "Received message validation failed: invalid_json",
            "error_details": {"reason": "invalid_json", "field": "body"},
        },
        {
            "event": "acquisition_message_processing_completed",
            "level": "info",
            "log_policy": "external_content_fetch",
            "service": "article_fetch",
            "stage": "acquisition",
            "request_id": "request-001",
            "environment": "test",
            "message_id": "following",
            "acquisition_request_id": "high/2026-09-15T00:00:00Z/1",
            "source_id": 1,
            "outcome": "acquired",
            "created_count": 0,
            "duration_ms": 125.0,
            "message_disposition": "completed",
        },
    ]


def test_mixed_batch_redelivers_only_retry_results_and_call_failures(
    runtime, read_logs
):
    """混在バッチで呼び出し例外の後も処理を続け、再配信対象のIDだけを入力順で返す。"""
    runtime.consumer.consume.side_effect = [
        AcquisitionSucceeded(1),
        RetryAcquisition(RuntimeError("retry"), code="unknown"),
        RuntimeError("consume-failed"),
        NoRetryAcquisition(cause="inactive"),
        NoRetryAcquisition(cause=RuntimeError("no-retry"), code="unknown"),
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
    terminal_logs = [
        log
        for log in read_logs()
        if log["event"] != "acquisition_message_processing_started"
    ]
    assert [log["message_id"] for log in terminal_logs] == message_ids
    assert terminal_logs[-1]["created_count"] == 2


def test_diagnostic_failure_preserves_retry_result(runtime, broken_output):
    """再配信と決めた結果はログ出力が失敗しても再配信対象から外さない。"""
    runtime.consumer.consume.return_value = RetryAcquisition(
        RuntimeError("retry"), code="unknown"
    )

    response = entrypoint.handler(
        {"Records": [{"messageId": "retry", "body": json.dumps(message())}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
