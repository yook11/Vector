"""Assessment Lambdaの接続と応答を、既存部品の詳細テストと分けて確認する。"""

import asyncio
import json
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.lambda_handlers.assessment import handler
from app.lambda_handlers.sqs.errors import SqsInputError

module = import_module("app.lambda_handlers.assessment.handler")
pytestmark = pytest.mark.unit


def valid_body(**changes) -> str:
    event = {
        "event_id": str(UUID(int=1)),
        "event_type": "article.curated_signal",
        "schema_version": 1,
        "occurred_at": "2026-09-12T00:00:00Z",
        "payload": {"curation_id": 1, "analyzable_article_id": 101},
    }
    event.update(changes)
    return json.dumps(event)


def record(index, **changes):
    return {
        "messageId": f" msg-{index} ",
        "body": valid_body(
            event_id=str(UUID(int=index)),
            payload={"curation_id": index, "analyzable_article_id": index + 100},
            **changes,
        ),
    }


@pytest.fixture
def wiring(monkeypatch):
    state = SimpleNamespace(
        order=[], consumer=SimpleNamespace(consume=AsyncMock()), settings=object()
    )

    @asynccontextmanager
    async def open_consumer(settings):
        assert settings is state.settings
        state.order.append("open")
        try:
            yield state.consumer
        finally:
            state.order.append("close")

    state.open = Mock(side_effect=open_consumer)
    state.settings_factory = Mock(return_value=state.settings)
    state.log = Mock()
    monkeypatch.setattr(module, "open_assessment_consumer", state.open)
    monkeypatch.setattr(module, "AssessmentConsumerSettings", state.settings_factory)
    monkeypatch.setattr(module, "logger", state.log)
    state.setup_logging = Mock()
    monkeypatch.setattr(module, "setup_lambda_logging", state.setup_logging)
    return state


def test_batch_reports_only_failed_message_ids(wiring):
    """4件すべてを処理し、失敗した2件のメッセージIDだけを返す。"""
    body = valid_body()
    messages = [
        {"messageId": "msg-1", "body": body},
        {"messageId": "msg-2", "body": body},
        {"messageId": "msg-3", "body": body},
        {"messageId": "msg-4", "body": body},
    ]
    # 入力順に、1件目と3件目は成功、2件目と4件目は処理失敗とする。
    wiring.consumer.consume.side_effect = [
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 901),
        RuntimeError("second-message-failed"),
        AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE),
        RuntimeError("fourth-message-failed"),
    ]

    response = handler({"Records": messages}, None)

    assert wiring.consumer.consume.await_count == 4
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "msg-2"},
            {"itemIdentifier": "msg-4"},
        ]
    }


@pytest.mark.parametrize("invalid", [False, True])
def test_batch_validation_follows_preparation_and_precedes_consumption(wiring, invalid):
    """空バッチもID不正も資源準備後に判定し、構造不正では一件もConsumerへ渡さない。"""
    records = [record(1), {"body": "private-body"}] if invalid else []
    if invalid:
        with pytest.raises(SqsInputError):
            handler({"Records": records}, None)
        wiring.log.warning.assert_called_once_with(
            "assessment_sqs_input_invalid",
            reason="missing_required_field",
            field="messageId",
            record_index=1,
        )
    else:
        assert handler({"Records": records}, None) == {"batchItemFailures": []}
        wiring.log.warning.assert_not_called()
    assert wiring.order == ["open", "close"]
    wiring.consumer.consume.assert_not_awaited()


def test_settings_failure_is_recorded_once_and_preserves_exception(wiring):
    """設定失敗をsettings段階で一度だけ記録し、ログ障害でも同じ例外を伝える。"""
    original = RuntimeError("private-settings")
    wiring.settings_factory.side_effect = original
    wiring.log.warning.side_effect = RuntimeError("private-log")
    with pytest.raises(RuntimeError) as caught:
        handler({"Records": []}, None)
    assert caught.value is original
    wiring.setup_logging.assert_called_once_with()
    wiring.open.assert_not_called()
    wiring.log.warning.assert_called_once_with(
        "assessment_initialization_failed",
        stage="settings",
        error_class="builtins.RuntimeError",
    )


def test_composition_failure_is_not_recorded_again_at_entry(wiring):
    """compositionが担当する初期化失敗を入口で再分類・二重記録しない。"""
    original = RuntimeError("private-composition")

    @asynccontextmanager
    async def fail(settings):
        raise original
        yield  # pragma: no cover

    wiring.open.side_effect = fail
    with pytest.raises(RuntimeError) as caught:
        handler({"Records": []}, None)
    assert caught.value is original
    wiring.log.warning.assert_not_called()
    wiring.consumer.consume.assert_not_awaited()


def test_unexpected_parser_failure_is_local_and_has_no_event_ids(wiring, monkeypatch):
    """検証済みイベントがない解析障害はmessageIdと例外型だけを記録し、次を処理する。"""
    real_parser = module.parse_curated_signal_event

    def parse(body):
        if body == "private-unexpected":
            raise RuntimeError("private-parser")
        return real_parser(body)

    monkeypatch.setattr(module, "parse_curated_signal_event", parse)
    wiring.consumer.consume.return_value = AssessmentCompletion(
        AssessmentCompletionKind.OUT_OF_SCOPE
    )
    result = handler(
        {"Records": [{"messageId": "bad", "body": "private-unexpected"}, record(1)]},
        None,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    wiring.log.warning.assert_called_once_with(
        "assessment_message_failed",
        message_id="bad",
        error_class="builtins.RuntimeError",
    )
    wiring.consumer.consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_processing_cancellation_propagates_after_leaving_scope(wiring):
    """処理中キャンセルを個別失敗へ変換せず、借用範囲を閉じて伝播する。"""
    original = asyncio.CancelledError()
    wiring.consumer.consume.side_effect = original
    with pytest.raises(asyncio.CancelledError) as caught:
        await module._run_assessment(
            {"Records": [record(1), record(2)]}, wiring.settings
        )
    assert caught.value is original
    assert wiring.order == ["open", "close"]
    wiring.consumer.consume.assert_awaited_once()
    wiring.log.warning.assert_not_called()
