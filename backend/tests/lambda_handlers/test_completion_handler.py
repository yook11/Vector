"""補完handlerの個別失敗・全体失敗と配送診断を確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
from app.lambda_handlers.sqs.errors import SqsInputError
from tests.collection.test_incomplete_article_recorded_event import valid_event

module = import_module("app.lambda_handlers.completion.handler")


def record(message_id="message", article_id=101):
    data = valid_event()
    data["payload"]["incomplete_article_id"] = article_id
    return {"messageId": message_id, "body": json.dumps(data)}


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=CompletionSucceeded(901))
        ),
        settings=object(),
        log=Mock(),
        released=False,
    )

    @asynccontextmanager
    async def open_resources(settings):
        try:
            yield SimpleNamespace(consumer=state.consumer, sqs_client=Mock())
        finally:
            state.released = True

    monkeypatch.setattr(module, "open_completion_resources", open_resources)
    state.settings_factory = Mock(return_value=state.settings)
    monkeypatch.setattr(module, "CompletionConsumerSettings", state.settings_factory)
    monkeypatch.setattr(module, "logger", state.log)
    monkeypatch.setattr(module, "setup_lambda_logging", Mock())
    return state


@pytest.mark.parametrize("reason", ["missing", "closed", "superseded", "url_conflict"])
def test_all_not_required_reasons_acknowledge(runtime, reason):
    """処理不要の理由によらず受信完了にする。"""
    runtime.consumer.consume.return_value = CompletionNotRequired(reason=reason)
    assert module.handler({"Records": [record()]}, None) == {"batchItemFailures": []}


def test_retry_decision_returns_message_for_redelivery(runtime):
    """Consumerの再試行判断は、当該メッセージの再配信になる。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("test failure"), RetryArticleCompletion("retry")
    )
    batch = {"Records": [record("retry-message", article_id=101)]}

    response = module.handler(batch, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry-message"}]}


def test_closed_decision_acknowledges_message(runtime):
    """Consumerがclosed確定を返したメッセージは受信完了になる。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("test failure"), CloseArticleCompletion("closed")
    )

    response = module.handler({"Records": [record("closed", article_id=101)]}, None)

    assert response == {"batchItemFailures": []}


def test_consumer_exception_returns_message_for_redelivery(runtime):
    """Consumer呼び出しの通常例外は、当該メッセージの再配信になる。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")
    batch = {"Records": [record("failed-message", article_id=101)]}

    response = module.handler(batch, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "failed-message"}]}


def test_message_exception_does_not_stop_next_article(runtime):
    """配送例外の後も、次の記事をConsumerへ渡す。"""
    runtime.consumer.consume.side_effect = [
        RuntimeError("test failure"),
        CompletionSucceeded(902),
    ]
    batch = {"Records": [record("failed", 101), record("success", 102)]}

    module.handler(batch, None)

    assert runtime.consumer.consume.await_args_list == [call(101), call(102)]


def test_failed_messages_follow_input_order(runtime):
    """失敗一覧はmessageIdのソート順ではなく、入力順を維持する。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")
    batch = {"Records": [record("z-first", 101), record("a-second", 102)]}

    response = module.handler(batch, None)

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "z-first"},
            {"itemIdentifier": "a-second"},
        ]
    }


def test_failed_message_id_preserves_whitespace(runtime):
    """再配信を要求するmessageIdは、受信した原文の空白を維持する。"""
    runtime.consumer.consume.side_effect = RuntimeError("test failure")

    response = module.handler({"Records": [record(" failed ")]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": " failed "}]}


def test_missing_body_does_not_reach_consumer(runtime):
    """本文のないメッセージはConsumerへ渡さず、再配信対象にする。"""
    response = module.handler({"Records": [{"messageId": "missing-body"}]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "missing-body"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_nontext_body_does_not_reach_consumer(runtime):
    """文字列でない本文はConsumerへ渡さず、再配信対象にする。"""
    response = module.handler(
        {"Records": [{"messageId": "object-body", "body": {}}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "object-body"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_invalid_json_does_not_reach_consumer(runtime):
    """解析できないJSONはConsumerへ渡さず、再配信対象にする。"""
    response = module.handler(
        {"Records": [{"messageId": "invalid-json", "body": "{invalid"}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid-json"}]}
    runtime.consumer.consume.assert_not_awaited()


def test_unsupported_version_does_not_reach_consumer(runtime):
    """非対応versionはConsumerへ渡さず、再配信対象にする。"""
    body = json.dumps({**valid_event(), "schema_version": 2})
    response = module.handler(
        {"Records": [{"messageId": "unsupported-version", "body": body}]}, None
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

    module.handler(batch, None)

    runtime.consumer.consume.assert_awaited_once_with(102)


def test_batch_identity_failure_precedes_any_consumption(runtime):
    """後続のID重複を、先頭の記事に着手する前に検出する。"""
    with pytest.raises(SqsInputError):
        module.handler({"Records": [record("same"), record("same", 102)]}, None)
    runtime.consumer.consume.assert_not_awaited()
    assert runtime.released


def test_empty_batch_acknowledges_without_consumption(runtime):
    """空の受信一覧は空の失敗応答になる。"""
    assert module.handler({"Records": []}, None) == {"batchItemFailures": []}
    runtime.consumer.consume.assert_not_awaited()


def test_settings_failure_fails_invocation(runtime):
    """設定不正を空の成功応答へ変換しない。"""
    runtime.settings_factory.side_effect = RuntimeError("private-settings")
    with pytest.raises(RuntimeError, match="private-settings"):
        module.handler({"Records": [record()]}, None)
    runtime.consumer.consume.assert_not_awaited()


def test_resource_initialization_failure_fails_invocation(runtime, monkeypatch):
    """初期化失敗を個別メッセージの失敗応答へ変換しない。"""

    @asynccontextmanager
    async def fail(settings):
        raise RuntimeError("private-init")
        yield

    monkeypatch.setattr(module, "open_completion_resources", fail)
    with pytest.raises(RuntimeError, match="private-init"):
        module.handler({"Records": [record()]}, None)
    runtime.consumer.consume.assert_not_awaited()


def test_cancellation_propagates_and_releases_resources(runtime):
    """キャンセル時は後続へ進まず、資源解放後にキャンセルを伝播する。"""
    runtime.consumer.consume.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        module.handler({"Records": [record(), record("after", 102)]}, None)
    runtime.consumer.consume.assert_awaited_once_with(101)
    assert runtime.released


def test_success_log_identifies_completed_article(runtime):
    """成功ログには検証済みのイベントと完成記事の識別子を記録する。"""
    runtime.consumer.consume.return_value = CompletionSucceeded(901)

    module.handler({"Records": [record("success", 101)]}, None)

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

    module.handler({"Records": [record("superseded")]}, None)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "not_required"
    assert fields["reason"] == "superseded"


def test_closed_log_exposes_decision_without_original_exception(runtime):
    """closedのログは判断codeと調査要否を記録し、元例外の自由文を出さない。"""
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-header"),
        CloseArticleCompletion("stop", requires_investigation=True),
    )

    module.handler({"Records": [record("closed")]}, None)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "closed"
    assert fields["code"] == "stop"
    assert fields["requires_investigation"] is True
    assert "private" not in repr(runtime.log.mock_calls)


def test_retry_log_preserves_original_time_and_safe_decision(runtime):
    """再試行ログは判断の元時刻を保持し、元例外の自由文を出さない。"""
    retry_at = datetime(2026, 9, 15, tzinfo=UTC)
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-header"),
        RetryArticleCompletion("retry", retry_at=retry_at, requires_investigation=True),
    )

    module.handler({"Records": [record("retry")]}, None)

    fields = runtime.log.info.call_args.kwargs
    assert fields["result"] == "retry"
    assert fields["code"] == "retry"
    assert fields["retry_at"] == retry_at.isoformat()
    assert fields["requires_investigation"] is True
    assert "private" not in repr(runtime.log.mock_calls)


def test_message_failure_log_does_not_expose_exception_text(runtime):
    """配送例外のログは識別子と例外型だけを出し、自由文を含めない。"""
    runtime.consumer.consume.side_effect = RuntimeError("private-exception")

    module.handler({"Records": [record("failed")]}, None)

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

    module.handler(batch, None)

    assert runtime.log.warning.call_args.kwargs["reason"] == "invalid_payload"
    assert "private" not in repr(runtime.log.mock_calls)


def test_success_log_failure_does_not_change_acknowledgement(runtime):
    """成功ログの出力失敗でも受信完了を維持する。"""
    runtime.log.info.side_effect = RuntimeError("private-log")

    response = module.handler({"Records": [record("saved")]}, None)

    assert response == {"batchItemFailures": []}


def test_retry_log_failure_does_not_change_redelivery(runtime):
    """再試行ログの出力失敗でも再配信対象を維持する。"""
    runtime.log.info.side_effect = RuntimeError("private-log")
    runtime.consumer.consume.return_value = CompletionFailed(
        RuntimeError("private-error"), RetryArticleCompletion("retry")
    )

    response = module.handler({"Records": [record("retry")]}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "retry"}]}


def test_invalid_input_log_failure_does_not_change_redelivery(runtime):
    """入力不正ログの出力失敗でも再配信対象を維持する。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
    batch = {"Records": [{"messageId": "invalid", "body": "private-body"}]}

    response = module.handler(batch, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}


def test_contract_validation_log_failure_preserves_redelivery(runtime):
    """イベント契約違反のログ障害でも再配信対象を維持する。"""
    runtime.log.warning.side_effect = RuntimeError("private-log")
    data = valid_event()
    data["schema_version"] = 2
    response = module.handler(
        {"Records": [{"messageId": "event", "body": json.dumps(data)}]}, None
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "event"}]}
