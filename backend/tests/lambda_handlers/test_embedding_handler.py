"""Embedding入口の入力の受け渡し・失敗範囲・処理順を確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.service import EmbeddingCompletion
from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason

module = import_module("app.lambda_handlers.embedding.handler")
pytestmark = pytest.mark.unit


def valid_body(*, curation_id=11, analyzed_article_id=101):
    return json.dumps(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "article.assessed_in_scope",
            "schema_version": 1,
            "occurred_at": "2026-09-12T00:00:00Z",
            "payload": {
                "curation_id": curation_id,
                "analyzed_article_id": analyzed_article_id,
            },
        }
    )


@pytest.fixture
def wiring(monkeypatch):
    state = SimpleNamespace(
        order=[],
        settings=object(),
        consumer=SimpleNamespace(
            consume=AsyncMock(return_value=EmbeddingCompletion.SAVED)
        ),
        log=Mock(),
    )

    @asynccontextmanager
    async def open_consumer(settings):
        state.order.append("open")
        try:
            yield state.consumer
        finally:
            state.order.append("close")

    state.open = Mock(side_effect=open_consumer)
    monkeypatch.setattr(module, "open_embedding_consumer", state.open)
    state.settings_factory = Mock(return_value=state.settings)
    monkeypatch.setattr(module, "EmbeddingConsumerSettings", state.settings_factory)
    monkeypatch.setattr(module, "logger", state.log)
    monkeypatch.setattr(module, "setup_lambda_logging", Mock())
    return state


def test_batch_reports_only_failed_message_ids(wiring):
    """成功したメッセージを含めず、失敗したIDだけを入力順で返す。"""
    body = valid_body()
    messages = [
        {"messageId": "saved-before", "body": body},
        {"messageId": "failed-first", "body": body},
        {"messageId": "saved-after", "body": body},
        {"messageId": "failed-last", "body": body},
    ]
    wiring.consumer.consume.side_effect = [
        EmbeddingCompletion.SAVED,
        RuntimeError("first-failure"),
        EmbeddingCompletion.SAVED,
        RuntimeError("last-failure"),
    ]

    response = module.handler({"Records": messages}, None)

    assert wiring.consumer.consume.await_count == 4
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "failed-first"},
            {"itemIdentifier": "failed-last"},
        ]
    }


def test_each_payload_is_passed_to_the_shared_consumer(wiring):
    """異なる入力のpayloadを、バッチで共有するConsumerへそのまま渡す。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(curation_id=11, analyzed_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(curation_id=22, analyzed_article_id=202),
        },
    ]

    module.handler({"Records": messages}, None)

    wiring.open.assert_called_once_with(wiring.settings)
    assert wiring.consumer.consume.await_args_list == [
        call(ArticleAssessedInScope(curation_id=11, analyzed_article_id=101)),
        call(ArticleAssessedInScope(curation_id=22, analyzed_article_id=202)),
    ]


def test_empty_batch_completes_without_consumption(wiring):
    """空バッチはConsumerを実行せず、空の失敗一覧を返す。"""
    response = module.handler({"Records": []}, None)

    assert response == {"batchItemFailures": []}
    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings)


def test_invalid_message_id_rejects_batch_before_consumption(wiring):
    """後続のIDが欠けていたら、先行分も処理せずバッチ全体を失敗にする。"""
    body = valid_body()
    messages = [{"messageId": "valid", "body": body}, {"body": body}]

    with pytest.raises(SqsInputError):
        module.handler({"Records": messages}, None)

    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings)
    wiring.log.warning.assert_called_once_with(
        "embedding_sqs_input_invalid",
        reason="missing_required_field",
        field="messageId",
        record_index=1,
    )


@pytest.mark.parametrize(
    "invalid_message",
    [
        pytest.param({"messageId": "invalid"}, id="missing-body"),
        pytest.param({"messageId": "invalid", "body": "not-json"}, id="invalid-json"),
        pytest.param(
            {"messageId": "invalid", "body": valid_body(curation_id=0)},
            id="invalid-event",
        ),
    ],
)
def test_invalid_message_does_not_prevent_following_message(wiring, invalid_message):
    """本文・イベント不正はそのメッセージだけの失敗とし、後続を処理する。"""
    messages = [invalid_message, {"messageId": "following", "body": valid_body()}]

    response = module.handler({"Records": messages}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    wiring.consumer.consume.assert_awaited_once_with(
        ArticleAssessedInScope(curation_id=11, analyzed_article_id=101)
    )


@pytest.mark.parametrize(
    "completion,reason",
    [
        (EmbeddingCompletion.SAVED, "saved"),
        (EmbeddingCompletion.ALREADY_EMBEDDED, "already_embedded"),
    ],
)
def test_completion_is_successful_with_its_reason(wiring, completion, reason):
    """Consumerの正常完了は失敗一覧に入れず、完了理由を記録する。"""
    wiring.consumer.consume.return_value = completion

    response = module.handler(
        {"Records": [{"messageId": "completed", "body": valid_body()}]}, None
    )

    assert response == {"batchItemFailures": []}
    wiring.log.info.assert_called_once_with(
        "embedding_message_completed",
        message_id="completed",
        event_id="00000000-0000-0000-0000-000000000001",
        analyzed_article_id=101,
        reason=reason,
    )


def test_message_logging_failure_does_not_change_batch_result(wiring):
    """成功・失敗の診断が壊れても、各メッセージの処理結果を変えない。"""
    body = valid_body()
    messages = [
        {"messageId": "failed", "body": body},
        {"messageId": "following", "body": body},
    ]
    wiring.consumer.consume.side_effect = [
        RuntimeError("processing-failed"),
        EmbeddingCompletion.SAVED,
    ]
    wiring.log.warning.side_effect = RuntimeError("warning-failed")
    wiring.log.info.side_effect = RuntimeError("info-failed")

    response = module.handler({"Records": messages}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "failed"}]}
    assert wiring.consumer.consume.await_count == 2


def test_input_logging_failure_does_not_replace_batch_error(wiring):
    """入力不正の診断が壊れても、バッチ検証の失敗をそのまま伝える。"""
    wiring.log.warning.side_effect = RuntimeError("logging-failed")

    with pytest.raises(SqsInputError):
        module.handler({"Records": [{"body": valid_body()}]}, None)

    wiring.consumer.consume.assert_not_awaited()


def test_unexpected_parser_failure_does_not_stop_batch(wiring, monkeypatch):
    """想定外の解析障害も、そのメッセージだけの失敗として後続を処理する。"""
    body = valid_body()
    parsed = module.parse_assessed_in_scope_event(body)
    monkeypatch.setattr(
        module,
        "parse_assessed_in_scope_event",
        Mock(side_effect=[RuntimeError("private-parser"), parsed]),
    )
    messages = [
        {"messageId": "parse-failed", "body": body},
        {"messageId": "following", "body": body},
    ]

    response = module.handler({"Records": messages}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "parse-failed"}]}
    wiring.consumer.consume.assert_awaited_once_with(parsed.payload)
    wiring.log.warning.assert_called_once_with(
        "embedding_message_failed",
        message_id="parse-failed",
        error_class="builtins.RuntimeError",
    )


def test_invalid_event_does_not_reuse_previous_event_in_diagnostics(wiring):
    """解析できないメッセージの診断に、直前のイベント情報や本文を混入させない。"""
    messages = [
        {"messageId": "saved", "body": valid_body()},
        {"messageId": "invalid", "body": "private-invalid-json"},
    ]

    module.handler({"Records": messages}, None)

    wiring.log.warning.assert_called_once_with(
        "embedding_message_input_invalid",
        message_id="invalid",
        reason="invalid_json",
        issues=[],
    )


def test_processing_failure_log_uses_only_verified_identifiers(wiring):
    """処理失敗の診断には検証済みの識別子を載せ、例外の自由文を残さない。"""
    wiring.consumer.consume.side_effect = RuntimeError("private-exception")

    module.handler({"Records": [{"messageId": "failed", "body": valid_body()}]}, None)

    wiring.log.warning.assert_called_once_with(
        "embedding_message_failed",
        message_id="failed",
        event_id="00000000-0000-0000-0000-000000000001",
        analyzed_article_id=101,
        error_class="builtins.RuntimeError",
    )


def test_initialization_log_failure_preserves_original_exception(wiring):
    """初期化失敗の診断が壊れても、元の例外を置き換えない。"""
    original = RuntimeError("settings-failed")
    wiring.settings_factory.side_effect = original
    wiring.log.warning.side_effect = RuntimeError("log-failed")

    with pytest.raises(RuntimeError) as caught:
        module.handler({"Records": []}, None)

    assert caught.value is original


@pytest.mark.asyncio
async def test_processing_cancellation_leaves_borrowed_scopes(wiring):
    """処理中キャンセルでも、準備した資源の利用範囲を閉じて伝播する。"""
    wiring.consumer.consume.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await module._run_embedding(
            {"Records": [{"messageId": "cancelled", "body": valid_body()}]},
            wiring.settings,
        )

    assert wiring.order == ["open", "close"]


def test_settings_failure_aborts_entire_batch(wiring):
    """設定失敗は個別応答にせず、settings段階を記録して元の例外を伝える。"""
    original = RuntimeError("private-settings")
    wiring.settings_factory.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler(
            {"Records": [{"messageId": "unprocessed", "body": valid_body()}]}, None
        )

    assert caught.value is original
    wiring.open.assert_not_called()
    wiring.log.warning.assert_called_once_with(
        "embedding_initialization_failed",
        stage="settings",
        error_class="builtins.RuntimeError",
    )


def test_composition_failure_is_not_recorded_again(wiring):
    """compositionが担当する初期化失敗を、入口で二重記録せず伝播する。"""
    original = RuntimeError("composition-failed")
    wiring.open.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler({"Records": []}, None)

    assert caught.value is original
    wiring.consumer.consume.assert_not_awaited()
    wiring.log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_messages_finish_sequentially_in_input_order(wiring):
    """先行メッセージの完了を待ってから、次のメッセージを処理する。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(curation_id=11, analyzed_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(curation_id=22, analyzed_article_id=202),
        },
    ]
    steps = []

    async def consume(payload):
        steps.append(("start", payload.curation_id))
        await asyncio.sleep(0)
        steps.append(("end", payload.curation_id))
        return EmbeddingCompletion.SAVED

    wiring.consumer.consume.side_effect = consume

    await module._run_embedding({"Records": messages}, wiring.settings)

    assert steps == [("start", 11), ("end", 11), ("start", 22), ("end", 22)]


def test_duplicate_id_is_rejected_before_reading_bodies(wiring):
    """本文欠落を個別処理する前に、重複IDをバッチ全体の失敗にする。"""
    messages = [
        {"messageId": "duplicate"},
        {"messageId": "duplicate", "body": valid_body()},
    ]

    with pytest.raises(SqsInputError) as caught:
        module.handler({"Records": messages}, None)

    assert caught.value.reason is SqsInputReason.DUPLICATE_MESSAGE_ID
    wiring.consumer.consume.assert_not_awaited()


def test_failed_message_id_is_not_trimmed(wiring):
    """失敗IDの前後の空白を除去せず、そのまま返す。"""
    wiring.consumer.consume.side_effect = RuntimeError("processing-failed")

    response = module.handler(
        {"Records": [{"messageId": " failed ", "body": valid_body()}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": " failed "}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_control_exception_stops_batch(wiring, interruption):
    """キャンセル・終了要求は個別失敗に変換せず、後続処理を止めて伝播する。"""
    wiring.consumer.consume.side_effect = interruption
    body = valid_body()
    messages = [
        {"messageId": "interrupted", "body": body},
        {"messageId": "unprocessed", "body": body},
    ]

    with pytest.raises(type(interruption)) as caught:
        await module._run_embedding({"Records": messages}, wiring.settings)

    assert caught.value is interruption
    wiring.consumer.consume.assert_awaited_once()
    wiring.log.warning.assert_not_called()
