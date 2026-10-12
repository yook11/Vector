"""Consumer・SQSをモックにし、補完handlerの配送判断と実ロガーの出力を確認する。"""

import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.collection.article_completion.consumer import (
    CompletionSucceeded,
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import ArticleCompletionError
from app.collection.retry_at import RetryAt
from app.http.errors import HttpResponseError
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.sqs.received_message import ReceivedMessageBatchInvalidError
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from tests.collection.test_incomplete_article_recorded_event import valid_event

module = import_module("app.lambda_handlers.completion.handler")


def record(message_id="message", article_id=101):
    data = valid_event()
    data["payload"]["incomplete_article_id"] = article_id
    return {
        "messageId": message_id,
        "body": json.dumps(data),
        "receiptHandle": " receipt-" + message_id + " ",
    }


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=CompletionSucceeded(901))
        ),
        settings=SimpleNamespace(
            env="test",
            sqs_article_completion_queue_url="https://sqs.ap-northeast-1.amazonaws.com/123456789012/completion",
        ),
        context=SimpleNamespace(
            aws_request_id="request-001",
            get_remaining_time_in_millis=Mock(return_value=600_000),
        ),
        sqs=Mock(),
        now=datetime(2026, 9, 14, tzinfo=UTC),
        released=False,
    )

    @asynccontextmanager
    async def open_resources(settings, *, failure_recorder):
        try:
            yield SimpleNamespace(consumer=state.consumer, sqs_client=state.sqs)
        finally:
            state.released = True

    monkeypatch.setattr(module, "open_completion_resources", open_resources)
    state.settings_factory = Mock(return_value=state.settings)
    monkeypatch.setattr(module, "CompletionConsumerSettings", state.settings_factory)
    clock = Mock(wraps=datetime)
    clock.now.side_effect = lambda tz: state.now.astimezone(tz)
    monkeypatch.setattr(module, "datetime", clock)
    return state


@pytest.fixture
def read_logs(capsys) -> Callable[[], list[dict[str, object]]]:
    """出力されたログを、実行ごとに変わる時刻・経過時間・発生位置を除いて返す。"""

    def _read() -> list[dict[str, object]]:
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        for log in logs:
            del log["timestamp"]
            log.pop("duration_ms", None)
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


@pytest.mark.parametrize("reason", ["missing", "closed", "superseded", "url_conflict"])
def test_all_not_required_reasons_acknowledge(runtime, reason):
    """処理不要の理由によらず受信完了にする。"""
    runtime.consumer.consume.return_value = NoRetryCompletion(cause=reason)
    assert module.handler({"Records": [record()]}, runtime.context) == {
        "batchItemFailures": []
    }


def test_retry_decision_returns_message_for_redelivery(runtime):
    """Consumerの再試行判断は、当該メッセージの再配信になる。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError("test failure"), "retry"
    )
    batch = {"Records": [record("retry-message", article_id=101)]}

    response = module.handler(batch, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry-message"}]}


def test_closed_decision_acknowledges_message(runtime):
    """Consumerがclosed確定を返したメッセージは受信完了になる。"""
    runtime.consumer.consume.return_value = NoRetryCompletion(
        RuntimeError("test failure"), "closed"
    )

    response = module.handler(
        {"Records": [record("closed", article_id=101)]}, runtime.context
    )

    assert response == {"batchItemFailures": []}


def test_consumer_exception_returns_message_for_redelivery(runtime):
    """Consumer呼び出しの通常例外は、当該メッセージの再配信になる。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")
    batch = {"Records": [record("failed-message", article_id=101)]}

    response = module.handler(batch, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "failed-message"}]}


def test_message_exception_does_not_stop_next_article(runtime):
    """配送例外の後も、次の記事をConsumerへ渡す。"""
    runtime.consumer.consume.side_effect = [
        RuntimeError("test failure"),
        CompletionSucceeded(902),
    ]
    batch = {"Records": [record("failed", 101), record("success", 102)]}

    module.handler(batch, runtime.context)

    assert runtime.consumer.consume.await_args_list == [call(101), call(102)]


def test_failed_messages_follow_input_order(runtime):
    """失敗一覧はmessageIdのソート順ではなく、入力順を維持する。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")
    batch = {"Records": [record("z-first", 101), record("a-second", 102)]}

    response = module.handler(batch, runtime.context)

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "z-first"},
            {"itemIdentifier": "a-second"},
        ]
    }


def test_failed_message_id_preserves_whitespace(runtime):
    """再配信を要求するmessageIdは、受信した原文の空白を維持する。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")

    response = module.handler({"Records": [record(" failed ")]}, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": " failed "}]}


def test_missing_body_does_not_reach_consumer(runtime):
    """本文のないメッセージはConsumerへ渡さず、再配信対象にする。"""
    response = module.handler(
        {"Records": [{"messageId": "missing-body"}]}, runtime.context
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "missing-body"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_nontext_body_does_not_reach_consumer(runtime):
    """文字列でない本文はConsumerへ渡さず、再配信対象にする。"""
    response = module.handler(
        {"Records": [{"messageId": "object-body", "body": {}}]}, runtime.context
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "object-body"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_invalid_json_does_not_reach_consumer(runtime):
    """解析できないJSONはConsumerへ渡さず、再配信対象にする。"""
    response = module.handler(
        {"Records": [{"messageId": "invalid-json", "body": "{invalid"}]},
        runtime.context,
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid-json"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_unsupported_version_does_not_reach_consumer(runtime):
    """非対応versionはConsumerへ渡さず、再配信対象にする。"""
    body = json.dumps({**valid_event(), "schema_version": 2})
    response = module.handler(
        {"Records": [{"messageId": "unsupported-version", "body": body}]},
        runtime.context,
    )

    assert response == {
        "batchItemFailures": [{"itemIdentifier": "unsupported-version"}]
    }
    runtime.consumer.consume.assert_not_awaited()


def test_invalid_message_does_not_stop_next_article(runtime):
    """不正JSONの後も、次の正常記事をConsumerへ渡す。"""
    batch = {
        "Records": [
            {"messageId": "invalid", "body": "{invalid"},
            record("next", 102),
        ]
    }

    module.handler(batch, runtime.context)

    runtime.consumer.consume.assert_awaited_once_with(102)


def test_batch_identity_failure_precedes_any_consumption(runtime):
    """後続のID重複を、先頭の記事に着手する前に検出する。"""
    with pytest.raises(ReceivedMessageBatchInvalidError):
        module.handler(
            {"Records": [record("same"), record("same", 102)]}, runtime.context
        )
    runtime.consumer.consume.assert_not_awaited()
    assert runtime.released


def test_empty_batch_acknowledges_without_consumption(runtime):
    """空の受信一覧は空の失敗応答になる。"""
    assert module.handler({"Records": []}, runtime.context) == {"batchItemFailures": []}
    runtime.consumer.consume.assert_not_awaited()


def test_settings_failure_fails_invocation(runtime):
    """設定不正を空の成功応答へ変換しない。"""
    runtime.settings_factory.side_effect = RuntimeError("private-settings")
    with pytest.raises(RuntimeError, match="private-settings"):
        module.handler({"Records": [record()]}, runtime.context)
    runtime.consumer.consume.assert_not_awaited()


def test_resource_initialization_failure_fails_invocation(runtime, monkeypatch):
    """初期化失敗を個別メッセージの失敗応答へ変換しない。"""

    @asynccontextmanager
    async def fail(settings, *, failure_recorder):
        raise RuntimeError("private-init")
        yield

    monkeypatch.setattr(module, "open_completion_resources", fail)
    with pytest.raises(RuntimeError, match="private-init"):
        module.handler({"Records": [record()]}, runtime.context)
    runtime.consumer.consume.assert_not_awaited()


def test_cancellation_propagates_and_releases_resources(runtime):
    """キャンセル時は後続へ進まず、資源解放後にキャンセルを伝播する。"""
    runtime.consumer.consume.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        module.handler({"Records": [record(), record("after", 102)]}, runtime.context)
    runtime.consumer.consume.assert_awaited_once_with(101)
    assert runtime.released


def test_success_log_identifies_completed_article(runtime, read_logs):
    """補完して保存したメッセージは、完成記事のIDと受信完了の扱いを完了ログに記録する。"""
    runtime.consumer.consume.return_value = CompletionSucceeded(901)

    module.handler({"Records": [record("success", 101)]}, runtime.context)

    assert read_logs()[-1] == {
        "event": "completion_message_processing_completed",
        "level": "info",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "completion",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "success",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "incomplete_article_id": 101,
        "source_id": 7,
        "outcome": "succeeded",
        "analyzable_article_id": 901,
        "message_disposition": "completed",
    }


@pytest.mark.parametrize("reason", ["missing", "closed", "superseded", "url_conflict"])
def test_not_required_log_retains_reason(runtime, read_logs, reason):
    """処理が不要だったメッセージは、Consumerが返した理由を完了ログのoutcomeに記録する。"""
    runtime.consumer.consume.return_value = NoRetryCompletion(cause=reason)

    module.handler({"Records": [record("not-required")]}, runtime.context)

    assert read_logs()[-1] == {
        "event": "completion_message_processing_completed",
        "level": "info",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "completion",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "not-required",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "incomplete_article_id": 101,
        "source_id": 7,
        "outcome": reason,
        "message_disposition": "completed",
    }


def test_closed_log_keeps_decision_and_cause_facts(runtime, read_logs):
    """再試行しない失敗は、判断のcodeと受信完了の扱いに、原因の説明と診断情報を添えて記録する。"""
    runtime.consumer.consume.return_value = NoRetryCompletion(
        ArticleCompletionError(
            cause=HttpResponseError(
                status_code=404, received_at=datetime(2026, 9, 14, tzinfo=UTC)
            )
        ),
        "http_response_error",
    )

    module.handler({"Records": [record("closed")]}, runtime.context)

    assert read_logs()[-1] == {
        "event": "completion_message_processing_failed",
        "level": "warning",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "completion",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "closed",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "incomplete_article_id": 101,
        "source_id": 7,
        "code": "http_response_error",
        "requires_investigation": False,
        "message_disposition": "completed",
        "error_class": (
            "app.collection.article_completion.errors.ArticleCompletionError"
        ),
        "error_message": "記事の補完工程で失敗しました",
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": "app.http.errors.HttpResponseError",
                    "error_message": "HTTPの非成功応答を受け取りました",
                    "frames": [],
                    "error_details": {"status_code": 404},
                },
            }
        ],
    }


def test_retry_log_preserves_original_time_and_decision(runtime, read_logs):
    """再試行する失敗は、判断の元時刻と再配信の扱いを、原因の診断情報とともに記録する。"""
    retry_at = RetryAt(datetime(2026, 9, 15, tzinfo=UTC))
    runtime.consumer.consume.return_value = RetryCompletion(
        ArticleCompletionError(
            cause=HttpResponseError(
                status_code=503,
                received_at=datetime(2026, 9, 14, tzinfo=UTC),
                retry_after="86400",
            )
        ),
        "http_response_error",
        retry_at=retry_at,
    )

    module.handler({"Records": [record("retry")]}, runtime.context)

    # 再試行の記録の後に、再配信の待機のログが続く。
    failed = [
        log
        for log in read_logs()
        if log["event"] == "completion_message_processing_failed"
    ]
    assert failed == [
        {
            "event": "completion_message_processing_failed",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "service": "article_fetch",
            "stage": "completion",
            "request_id": "request-001",
            "environment": "test",
            "message_id": "retry",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "incomplete_article_id": 101,
            "source_id": 7,
            "code": "http_response_error",
            "requires_investigation": False,
            "retry_at": "2026-09-15T00:00:00+00:00",
            "message_disposition": "batch_item_failure",
            "error_class": (
                "app.collection.article_completion.errors.ArticleCompletionError"
            ),
            "error_message": "記事の補完工程で失敗しました",
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "app.http.errors.HttpResponseError",
                        "error_message": "HTTPの非成功応答を受け取りました",
                        "frames": [],
                        "error_details": {"status_code": 503},
                    },
                }
            ],
        }
    ]


def test_consumer_exception_is_logged_as_call_failure(runtime, read_logs):
    """Consumerから結果を受け取れなかった例外は、呼び出しの失敗として再配信の扱いとともに記録する。"""
    runtime.consumer.consume.side_effect = RuntimeError("consume-raised")

    module.handler({"Records": [record("failed")]}, runtime.context)

    assert read_logs()[-1] == {
        "event": "completion_message_processing_failed",
        "level": "error",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "completion",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "failed",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "incomplete_article_id": 101,
        "source_id": 7,
        "operation": "consume",
        "message_disposition": "batch_item_failure",
        "error_class": "builtins.RuntimeError",
        "error_message": "consume-raised",
    }


def test_invalid_event_log_does_not_expose_body_or_receipt(runtime, read_logs):
    """契約違反のイベントは検証の理由と項目だけを記録し、本文の値・未知の項目名・receiptHandleを出さない。"""
    invalid = valid_event()
    invalid["payload"]["private-key"] = "private-value"
    batch = {
        "Records": [
            {
                "messageId": "invalid",
                "body": json.dumps(invalid),
                "receiptHandle": "private-receipt",
            }
        ]
    }

    module.handler(batch, runtime.context)

    assert read_logs()[-1] == {
        "event": "completion_message_processing_failed",
        "level": "error",
        "log_policy": "external_content_fetch",
        "service": "article_fetch",
        "stage": "completion",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "invalid",
        "operation": "validate_event",
        "message_disposition": "batch_item_failure",
        "error_class": (
            "app.collection.article_acquisition.events."
            "IncompleteArticleEventInvalidError"
        ),
        "error_message": "Validation failed: invalid_payload",
        "error_details": {
            "kind": "application_validation",
            "reason": "invalid_payload",
            "issues": [{"field": "payload", "code": "unknown_field"}],
        },
    }


def test_success_log_failure_does_not_change_acknowledgement(runtime, broken_output):
    """成功ログの出力失敗でも受信完了を維持する。"""
    response = module.handler({"Records": [record("saved")]}, runtime.context)

    assert response == {"batchItemFailures": []}


def test_retry_log_failure_does_not_change_redelivery(runtime, broken_output):
    """再試行ログの出力失敗でも再配信対象を維持する。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError("private-error"), "retry"
    )

    response = module.handler({"Records": [record("retry")]}, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}


def test_invalid_input_log_failure_does_not_change_redelivery(runtime, broken_output):
    """入力不正ログの出力失敗でも再配信対象を維持する。"""
    batch = {"Records": [{"messageId": "invalid", "body": "private-body"}]}

    response = module.handler(batch, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}


def test_contract_validation_log_failure_preserves_redelivery(runtime, broken_output):
    """イベント契約違反のログ障害でも再配信対象を維持する。"""
    data = valid_event()
    data["schema_version"] = 2
    response = module.handler(
        {"Records": [{"messageId": "event", "body": json.dumps(data)}]}, runtime.context
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "event"}]}


@pytest.mark.parametrize("millis,started", [(60_000, True), (59_999, False)])
def test_article_start_boundary(runtime, millis, started):
    """残り60秒以上のときだけ記事を開始する。"""
    runtime.context.get_remaining_time_in_millis.return_value = millis
    response = module.handler({"Records": [record()]}, runtime.context)
    assert runtime.consumer.consume.await_count == int(started)
    assert response["batchItemFailures"] == (
        [] if started else [{"itemIdentifier": "message"}]
    )


def test_unstarted_messages_keep_input_order_after_previous_failure(runtime):
    """途中で打ち切っても既存失敗と未着手分を原文・入力順で一度ずつ返す。"""
    runtime.context.get_remaining_time_in_millis.side_effect = [60_000, 59_999]
    runtime.consumer.consume.side_effect = RuntimeError("article failure")
    response = module.handler(
        {"Records": [record(" z "), record(" a ", 102), record(" b ", 103)]},
        runtime.context,
    )
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": name} for name in (" z ", " a ", " b ")
        ]
    }
    runtime.consumer.consume.assert_awaited_once_with(101)


def test_insufficient_time_precedes_body_parsing(runtime, read_logs):
    """最初から時間不足なら本文不正として解析しない。"""
    runtime.context.get_remaining_time_in_millis.return_value = 59_999
    response = module.handler(
        {"Records": [{"messageId": "first"}, {"messageId": "next"}]}, runtime.context
    )
    assert response["batchItemFailures"] == [
        {"itemIdentifier": "first"},
        {"itemIdentifier": "next"},
    ]
    assert [log["event"] for log in read_logs()] == [
        "completion_message_unstarted",
        "completion_message_unstarted",
    ]


def test_all_ids_are_checked_before_remaining_time(runtime):
    """時間不足でも後続のID不正を呼び出し全体の失敗にする。"""
    runtime.context.get_remaining_time_in_millis.return_value = 0
    with pytest.raises(ReceivedMessageBatchInvalidError):
        module.handler({"Records": [record(), {"body": "invalid"}]}, runtime.context)
    runtime.context.get_remaining_time_in_millis.assert_not_called()


def test_remaining_time_failure_propagates_and_releases(runtime):
    """残り時間取得の通常障害は資源解放後に伝播する。"""
    runtime.context.get_remaining_time_in_millis.side_effect = RuntimeError(
        "clock failure"
    )
    with pytest.raises(RuntimeError, match="clock failure"):
        module.handler({"Records": [record()]}, runtime.context)
    assert runtime.released


def test_waits_are_applied_after_all_article_processing(runtime):
    """待機変更は後続の記事処理による時間経過を反映する。"""
    retry = RetryAt(runtime.now + timedelta(seconds=120))

    async def consume(article_id):
        if article_id == 101:
            return RetryCompletion(RuntimeError(), "retry", retry)
        runtime.sqs.change_message_visibility.assert_not_called()
        runtime.now += timedelta(seconds=30)
        return CompletionSucceeded(902)

    runtime.consumer.consume.side_effect = consume
    response = module.handler(
        {"Records": [record("retry"), record("success", 102)]}, runtime.context
    )
    runtime.sqs.change_message_visibility.assert_called_once_with(
        QueueUrl=runtime.settings.sqs_article_completion_queue_url,
        ReceiptHandle=" receipt-retry ",
        VisibilityTimeout=90,
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}


def test_wait_clock_failure_is_invocation_failure(runtime):
    """待機段階で残り時間を取得できない場合も全体失敗にする。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )
    runtime.context.get_remaining_time_in_millis.side_effect = [
        60_000,
        RuntimeError("clock failure"),
    ]
    with pytest.raises(RuntimeError, match="clock failure"):
        module.handler({"Records": [record()]}, runtime.context)


@pytest.mark.parametrize("sqs_error", [None, RuntimeError("private-sqs")])
def test_wait_outcome_never_removes_retry_failure(runtime, sqs_error):
    """可視性変更の成否によらず対象を再配信として返す。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )
    runtime.sqs.change_message_visibility.side_effect = sqs_error
    assert module.handler({"Records": [record(" retry ")]}, runtime.context) == {
        "batchItemFailures": [{"itemIdentifier": " retry "}]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_error", [None, RuntimeError("private-sdk")])
async def test_cancelled_communication_finishes_before_resource_release(
    runtime, communication_error
):
    """キャンセルが重なっても通信終了まで資源を保ち、元のキャンセルを伝播する。"""
    import threading

    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )

    def send(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(timeout=5), "テスト側が通信を解放しなかった"
        assert not runtime.released
        if communication_error:
            raise communication_error

    runtime.sqs.change_message_visibility.side_effect = send
    logger = create_policy_json_logger("article_fetch", EXTERNAL_CONTENT_LOG_RULES)
    task = asyncio.create_task(
        module._run_completion(
            {"Records": [record(), record("second", 102)]},
            runtime.settings,
            context=runtime.context,
            now=lambda: runtime.now,
            logger=logger,
            failure_recorder=ArticleFetchLifecycleRecorder(logger, stage="completion"),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not runtime.released
        assert not task.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runtime.released
    assert runtime.sqs.change_message_visibility.call_count == 1


def test_unstarted_tail_still_allows_wait_for_processed_retry(runtime):
    """記事開始を打ち切った後も予算内なら先行記事の待機を設定する。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )
    runtime.context.get_remaining_time_in_millis.side_effect = [60_000, 59_999, 20_000]
    response = module.handler(
        {"Records": [record("retry"), record("unstarted", 102)]}, runtime.context
    )
    runtime.sqs.change_message_visibility.assert_called_once()
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "retry"},
            {"itemIdentifier": "unstarted"},
        ]
    }


def test_skipped_wait_keeps_retry_in_response(runtime):
    """待機設定の時間不足を受信完了へ変換しない。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )
    runtime.context.get_remaining_time_in_millis.side_effect = [60_000, 19_999]
    response = module.handler({"Records": [record("retry")]}, runtime.context)
    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
    runtime.sqs.change_message_visibility.assert_not_called()


def test_wait_logging_failure_preserves_response(runtime, broken_output):
    """待機結果の診断障害で再配信対象を増減させない。"""
    runtime.consumer.consume.return_value = RetryCompletion(
        RuntimeError(), "retry", RetryAt(runtime.now + timedelta(seconds=120))
    )
    assert module.handler({"Records": [record()]}, runtime.context) == {
        "batchItemFailures": [{"itemIdentifier": "message"}],
    }


def test_unstarted_logging_failure_preserves_all_unstarted_ids(runtime, broken_output):
    """未着手の診断障害でも未着手分をすべて再配信にする。"""
    runtime.context.get_remaining_time_in_millis.return_value = 59_999
    assert module.handler(
        {"Records": [record("first"), record("second", 102)]}, runtime.context
    ) == {
        "batchItemFailures": [
            {"itemIdentifier": "first"},
            {"itemIdentifier": "second"},
        ],
    }
