"""補完handlerの個別失敗・全体失敗と配送診断を確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.collection.article_completion.consumer import (
    CompletionFailed,
    CompletionNotRequired,
    CompletionSucceeded,
)
from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)
from app.collection.retry_at import RetryAt
from app.lambda_handlers.sqs.errors import SqsInputError
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
            sqs_article_completion_queue_url="https://sqs.ap-northeast-1.amazonaws.com/123456789012/completion"
        ),
        context=SimpleNamespace(
            get_remaining_time_in_millis=Mock(return_value=600_000)
        ),
        sqs=Mock(),
        now=datetime(2026, 9, 14, tzinfo=UTC),
        log=Mock(),
        released=False,
    )

    @asynccontextmanager
    async def open_resources(settings):
        try:
            yield SimpleNamespace(consumer=state.consumer, sqs_client=state.sqs)
        finally:
            state.released = True

    monkeypatch.setattr(module, "open_completion_resources", open_resources)
    state.settings_factory = Mock(return_value=state.settings)
    monkeypatch.setattr(module, "CompletionConsumerSettings", state.settings_factory)
    monkeypatch.setattr(module, "logger", state.log)
    monkeypatch.setattr(module, "setup_lambda_logging", Mock())
    clock = Mock(wraps=datetime)
    clock.now.side_effect = lambda tz: state.now.astimezone(tz)
    monkeypatch.setattr(module, "datetime", clock)
    return state


@pytest.mark.parametrize("reason", ["missing", "closed", "superseded", "url_conflict"])
def test_all_not_required_reasons_acknowledge(runtime, reason):
    """処理不要の理由によらず受信完了にする。"""
    runtime.consumer.consume.return_value = CompletionNotRequired(reason=reason)
    assert module.handler({"Records": [record()]}, runtime.context) == {
        "batchItemFailures": []
    }


def test_retry_decision_returns_message_for_redelivery(runtime):
    """Consumerの再試行判断は、当該メッセージの再配信になる。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("test failure"), RetryArticleCompletion("retry")
    )
    batch = {"Records": [record("retry-message", article_id=101)]}

    response = module.handler(batch, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry-message"}]}


def test_closed_decision_acknowledges_message(runtime):
    """Consumerがclosed確定を返したメッセージは受信完了になる。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("test failure"), CloseArticleCompletion("closed")
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
    with pytest.raises(SqsInputError):
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
    async def fail(settings):
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


def test_success_log_identifies_completed_article(runtime):
    """成功ログには検証済みのイベントと完成記事の識別子を記録する。"""
    runtime.consumer.consume.return_value = CompletionSucceeded(901)

    module.handler({"Records": [record("success", 101)]}, runtime.context)

    assert runtime.log.info.call_args.kwargs == {
        "message_id": "success",
        "event_id": valid_event()["event_id"],
        "incomplete_article_id": 101,
        "analyzable_article_id": 901,
        "result": "succeeded",
    }


def test_not_required_log_retains_reason(runtime):
    """処理不要のログにはConsumerが返した理由を記録する。"""
    runtime.consumer.consume.return_value = CompletionNotRequired("superseded")

    module.handler({"Records": [record("superseded")]}, runtime.context)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "not_required"
    assert fields["reason"] == "superseded"


def test_closed_log_exposes_decision_without_original_exception(runtime):
    """closedのログは判断codeと調査要否を記録し、元例外の自由文を出さない。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-header"),
        CloseArticleCompletion("stop", requires_investigation=True),
    )

    module.handler({"Records": [record("closed")]}, runtime.context)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "closed"
    assert fields["code"] == "stop"
    assert fields["requires_investigation"] is True
    assert "private" not in repr(runtime.log.mock_calls)


def test_retry_log_preserves_original_time_and_safe_decision(runtime):
    """再試行ログは判断の元時刻を保持し、元例外の自由文を出さない。"""
    retry_at = RetryAt(datetime(2026, 9, 15, tzinfo=UTC))
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-header"),
        RetryArticleCompletion("retry", retry_at=retry_at, requires_investigation=True),
    )

    module.handler({"Records": [record("retry")]}, runtime.context)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "retry"
    assert fields["code"] == "retry"
    assert fields["retry_at"] == retry_at.value.isoformat()
    assert fields["requires_investigation"] is True
    assert "private" not in repr(runtime.log.mock_calls)


def test_message_failure_log_does_not_expose_exception_text(runtime):
    """配送例外のログは識別子と例外型だけを出し、自由文を含めない。"""
    runtime.consumer.consume.side_effect = RuntimeError("private-exception")

    module.handler({"Records": [record("failed")]}, runtime.context)

    assert set(runtime.log.warning.call_args.kwargs) == {
        "message_id",
        "event_id",
        "incomplete_article_id",
        "error_class",
    }
    assert "private" not in repr(runtime.log.mock_calls)


def test_invalid_event_log_does_not_expose_body_or_receipt(runtime):
    """入力不正のログに本文・未知の項目名・receiptHandleを出さない。"""
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

    assert runtime.log.warning.call_args.kwargs["reason"] == "invalid_payload"
    assert "private" not in repr(runtime.log.mock_calls)


def test_success_log_failure_does_not_change_acknowledgement(runtime):
    """成功ログの出力失敗でも受信完了を維持する。"""
    runtime.log.info.side_effect = RuntimeError("private-log")

    response = module.handler({"Records": [record("saved")]}, runtime.context)

    assert response == {"batchItemFailures": []}


def test_retry_log_failure_does_not_change_redelivery(runtime):
    """再試行ログの出力失敗でも再配信対象を維持する。"""
    runtime.log.info.side_effect = RuntimeError("private-log")
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-error"), RetryArticleCompletion("retry")
    )

    response = module.handler({"Records": [record("retry")]}, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}


def test_invalid_input_log_failure_does_not_change_redelivery(runtime):
    """入力不正ログの出力失敗でも再配信対象を維持する。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
    batch = {"Records": [{"messageId": "invalid", "body": "private-body"}]}

    response = module.handler(batch, runtime.context)

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}


def test_contract_validation_log_failure_preserves_redelivery(runtime):
    """イベント契約違反のログ障害でも再配信対象を維持する。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
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


def test_insufficient_time_precedes_body_parsing(runtime):
    """最初から時間不足なら本文不正として解析しない。"""
    runtime.context.get_remaining_time_in_millis.return_value = 59_999
    response = module.handler(
        {"Records": [{"messageId": "first"}, {"messageId": "next"}]}, runtime.context
    )
    assert response["batchItemFailures"] == [
        {"itemIdentifier": "first"},
        {"itemIdentifier": "next"},
    ]
    assert all(
        c.args[0] == "completion_message_unstarted"
        for c in runtime.log.warning.call_args_list
    )


def test_all_ids_are_checked_before_remaining_time(runtime):
    """時間不足でも後続のID不正を呼び出し全体の失敗にする。"""
    runtime.context.get_remaining_time_in_millis.return_value = 0
    with pytest.raises(SqsInputError):
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
            return CompletionFailed(
                RuntimeError(), RetryArticleCompletion("retry", retry)
            )
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
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
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
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
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
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
    )

    def send(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(timeout=5), "テスト側が通信を解放しなかった"
        assert not runtime.released
        if communication_error:
            raise communication_error

    runtime.sqs.change_message_visibility.side_effect = send
    task = asyncio.create_task(
        module._run_completion(
            {"Records": [record(), record("second", 102)]},
            runtime.settings,
            context=runtime.context,
            now=lambda: runtime.now,
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
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
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
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
    )
    runtime.context.get_remaining_time_in_millis.side_effect = [60_000, 19_999]
    response = module.handler({"Records": [record("retry")]}, runtime.context)
    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
    runtime.sqs.change_message_visibility.assert_not_called()


def test_wait_logging_failure_preserves_response(runtime):
    """待機結果の診断障害で再配信対象を増減させない。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError(),
        RetryArticleCompletion("retry", RetryAt(runtime.now + timedelta(seconds=120))),
    )
    assert module.handler({"Records": [record()]}, runtime.context) == {
        "batchItemFailures": [{"itemIdentifier": "message"}],
    }


def test_unstarted_logging_failure_preserves_all_unstarted_ids(runtime):
    """未着手の診断障害でも未着手分をすべて再配信にする。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
    runtime.context.get_remaining_time_in_millis.return_value = 59_999
    assert module.handler(
        {"Records": [record("first"), record("second", 102)]}, runtime.context
    ) == {
        "batchItemFailures": [
            {"itemIdentifier": "first"},
            {"itemIdentifier": "second"},
        ],
    }
