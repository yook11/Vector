"""Curation入口の入力の受け渡し・失敗範囲・処理順と、開始・終端のログを確認する。"""

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest
import structlog

from app.ai_providers.errors import AIProviderResultError, AIProviderResultReason
from app.analysis.curation.consumer_failure_classification import (
    NoRetryCuration,
    RetryCuration,
)
from app.analysis.curation.domain.ready import (
    CurationReadyBuildRejected,
    CurationReadyBuildRejectionReason,
)
from app.analysis.curation.service import (
    CurationCompletion,
    CurationCompletionKind,
)
from app.analysis.logging import create_article_analysis_logger
from app.collection.events import AnalyzableArticleCreated
from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason
from app.lambda_handlers.sqs.records import SqsRecord

module = import_module("app.lambda_handlers.curation.handler")
pytestmark = pytest.mark.unit

_CONTEXT = SimpleNamespace(aws_request_id="request-001")


def valid_body(*, analyzable_article_id=101):
    return json.dumps(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "article.analyzable_created",
            "schema_version": 1,
            "occurred_at": "2026-09-12T00:00:00Z",
            "payload": {
                "analyzable_article_id": analyzable_article_id,
            },
        }
    )


@pytest.fixture
def wiring(monkeypatch):
    state = SimpleNamespace(
        order=[],
        settings=SimpleNamespace(aws_region="ap-northeast-1", env="test"),
        consumer=SimpleNamespace(
            consume=AsyncMock(
                return_value=CurationCompletion(CurationCompletionKind.SIGNAL, 901)
            )
        ),
        log=create_article_analysis_logger().bind(stage="curation"),
    )

    @asynccontextmanager
    async def open_consumer(settings, *, logger):
        state.order.append("open")
        try:
            yield state.consumer
        finally:
            state.order.append("close")

    state.open = Mock(side_effect=open_consumer)
    monkeypatch.setattr(module, "open_curation_consumer", state.open)
    state.settings_factory = Mock(return_value=state.settings)
    monkeypatch.setattr(module, "CurationConsumerSettings", state.settings_factory)
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


def test_handler_outputs_json_without_global_logging_configuration(
    wiring, monkeypatch, capsys
):
    """グローバル設定を使用・変更せず、目的別ロガーからJSONを出力する。"""
    configure = structlog.configure
    original_config = structlog.get_config().copy()

    def reject_global_processor(logger, method_name, event_dict):
        raise AssertionError("global processor must not be used")

    try:
        configure(processors=[reject_global_processor])
        global_config = structlog.get_config().copy()
        configure_spy = Mock(wraps=configure)
        with monkeypatch.context() as scoped:
            scoped.setattr(structlog, "configure", configure_spy)

            module.handler(
                {"Records": [{"messageId": "message-001", "body": valid_body()}]},
                _CONTEXT,
            )

            configure_spy.assert_not_called()
            assert structlog.get_config() == global_config

        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        completed = next(
            record
            for record in records
            if record["event"] == "curation_message_processing_completed"
        )
        assert completed["log_policy"] == "ai_inference"
        assert completed["message_id"] == "message-001"
    finally:
        configure(**original_config)


@pytest.mark.parametrize(
    ("completion", "result"),
    [
        pytest.param(
            CurationCompletion(CurationCompletionKind.SIGNAL, 901),
            {"outcome": "signal", "curation_id": 901},
            id="signal",
        ),
        pytest.param(
            CurationCompletion(CurationCompletionKind.NOISE),
            {"outcome": "noise"},
            id="noise",
        ),
        pytest.param(
            CurationCompletion(CurationCompletionKind.ALREADY_CURATED),
            {"outcome": "already_curated"},
            id="already_curated",
        ),
    ],
)
def test_completion_is_logged_after_start_with_its_outcome(
    wiring, read_logs, completion, result
):
    """正常完了は開始と完了を1件ずつ出し、完了に結果と受信完了の扱いを記録する。"""
    wiring.consumer.consume.return_value = completion

    module.handler(
        {"Records": [{"messageId": "completed", "body": valid_body()}]}, _CONTEXT
    )

    assert read_logs() == [
        {
            "event": "curation_message_processing_started",
            "level": "info",
            "log_policy": "ai_inference",
            "service": "article_analysis",
            "stage": "curation",
            "request_id": "request-001",
            "environment": "test",
            "message_id": "completed",
        },
        {
            "event": "curation_message_processing_completed",
            "level": "info",
            "log_policy": "ai_inference",
            "service": "article_analysis",
            "stage": "curation",
            "request_id": "request-001",
            "environment": "test",
            "message_id": "completed",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "analyzable_article_id": 101,
            "message_disposition": "completed",
            **result,
        },
    ]


@pytest.mark.parametrize(
    "reason",
    [
        CurationReadyBuildRejectionReason.ARTICLE_MISSING,
        CurationReadyBuildRejectionReason.INPUT_INVALID,
        CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE,
    ],
)
def test_ready_build_rejection_is_logged_as_failure_completed(
    wiring, read_logs, reason
):
    """前提不成立は失敗として拒否理由を記録し、受信完了として失敗一覧に含めない。"""
    wiring.consumer.consume.return_value = NoRetryCuration(
        CurationReadyBuildRejected(reason)
    )

    response = module.handler(
        {"Records": [{"messageId": "rejected", "body": valid_body()}]}, _CONTEXT
    )

    assert response == {"batchItemFailures": []}
    assert read_logs()[-1] == {
        "event": "curation_message_processing_failed",
        "level": "warning",
        "log_policy": "ai_inference",
        "service": "article_analysis",
        "stage": "curation",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "rejected",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "analyzable_article_id": 101,
        "operation": "build_ready",
        "rejection_code": reason.value,
        "message_disposition": "completed",
    }


def test_failure_not_retried_is_logged_as_failure_completed(wiring, read_logs):
    """再試行しないAIの失敗は、codeと理由を記録し、受信完了として失敗一覧に含めない。"""
    wiring.consumer.consume.return_value = NoRetryCuration(
        AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
    )

    response = module.handler(
        {"Records": [{"messageId": "not-retried", "body": valid_body()}]}, _CONTEXT
    )

    assert response == {"batchItemFailures": []}
    assert read_logs()[-1] == {
        "event": "curation_message_processing_failed",
        "level": "warning",
        "log_policy": "ai_inference",
        "service": "article_analysis",
        "stage": "curation",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "not-retried",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "analyzable_article_id": 101,
        "code": "ai_provider_result_error",
        "failure_reason": "input_blocked",
        "message_disposition": "completed",
    }


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(RetryCuration(RuntimeError("decided-retry")), id="retry"),
        pytest.param(RuntimeError("decided-retry"), id="raised"),
    ],
)
def test_retried_failure_is_logged_with_its_exception(wiring, read_logs, outcome):
    """再試行と決めた失敗と伝播した例外は、例外の診断を記録して失敗一覧に載せる。"""
    wiring.consumer.consume.side_effect = [outcome]

    response = module.handler(
        {"Records": [{"messageId": "retried", "body": valid_body()}]}, _CONTEXT
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "retried"}]}
    assert read_logs()[-1] == {
        "event": "curation_message_processing_failed",
        "level": "error",
        "log_policy": "ai_inference",
        "service": "article_analysis",
        "stage": "curation",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "retried",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "analyzable_article_id": 101,
        "message_disposition": "batch_item_failure",
        "error_class": "builtins.RuntimeError",
        "error_message": "decided-retry",
    }


@pytest.mark.parametrize(
    ("invalid_message", "failure"),
    [
        pytest.param(
            {"messageId": "invalid"},
            {
                "operation": "parse_message",
                "error_class": "app.lambda_handlers.sqs.errors.SqsInputError",
                "error_message": "SQS input validation failed: missing_required_field",
                "error_details": {"reason": "missing_required_field", "field": "body"},
            },
            id="missing-body",
        ),
        pytest.param(
            {"messageId": "invalid", "body": "private-invalid-json"},
            {
                "operation": "parse_message",
                "error_class": (
                    "app.lambda_handlers.sqs.errors.SqsMessageJsonInvalidError"
                ),
                "error_message": "SQS message JSON parsing failed",
                "error_details": {"reason": "invalid_json"},
            },
            id="invalid-json",
        ),
        pytest.param(
            {"messageId": "invalid", "body": valid_body(analyzable_article_id=0)},
            {
                "operation": "validate_event",
                "error_class": "app.collection.events.AnalyzableEventInvalidError",
                "error_message": "Validation failed: invalid_payload",
                "error_details": {
                    "kind": "application_validation",
                    "reason": "invalid_payload",
                    "issues": [
                        {
                            "field": "payload.analyzable_article_id",
                            "code": "invalid_value",
                        }
                    ],
                },
            },
            id="invalid-event",
        ),
    ],
)
def test_invalid_message_is_logged_without_previous_event(
    wiring, read_logs, invalid_message, failure
):
    """本文・イベント不正は失敗した処理と検証の診断を記録し、直前のイベントIDを持ち越さない。"""
    messages = [{"messageId": "saved", "body": valid_body()}, invalid_message]

    module.handler({"Records": messages}, _CONTEXT)

    assert read_logs()[-1] == {
        "event": "curation_message_processing_failed",
        "level": "warning",
        "log_policy": "ai_inference",
        "service": "article_analysis",
        "stage": "curation",
        "request_id": "request-001",
        "environment": "test",
        "message_id": "invalid",
        "message_disposition": "batch_item_failure",
        **failure,
    }


def test_settings_failure_is_logged_and_aborts_entire_batch(wiring, read_logs):
    """設定失敗は個別応答にせず、settingsの処理として記録して元の例外を伝える。"""
    original = RuntimeError("private-settings")
    wiring.settings_factory.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler(
            {"Records": [{"messageId": "unprocessed", "body": valid_body()}]},
            _CONTEXT,
        )

    assert caught.value is original
    wiring.open.assert_not_called()
    assert read_logs() == [
        {
            "event": "curation_initialization_failed",
            "level": "error",
            "log_policy": "ai_inference",
            "service": "article_analysis",
            "stage": "curation",
            "request_id": "request-001",
            "operation": "settings",
            "error_class": "builtins.RuntimeError",
            "error_message": "private-settings",
        }
    ]


def test_invalid_message_id_is_logged_and_rejects_batch(wiring, read_logs):
    """後続のIDが欠けていたら、入力不正を記録し、先行分も処理せずバッチ全体を失敗にする。"""
    body = valid_body()
    messages = [{"messageId": "valid", "body": body}, {"body": body}]

    with pytest.raises(SqsInputError):
        module.handler({"Records": messages}, _CONTEXT)

    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings, logger=ANY)
    assert wiring.order == ["open", "close"]
    assert read_logs() == [
        {
            "event": "curation_sqs_input_invalid",
            "level": "warning",
            "log_policy": "ai_inference",
            "service": "article_analysis",
            "stage": "curation",
            "request_id": "request-001",
            "environment": "test",
            "error_class": "app.lambda_handlers.sqs.errors.SqsInputError",
            "error_message": "SQS input validation failed: missing_required_field",
            "error_details": {
                "reason": "missing_required_field",
                "field": "messageId",
                "record_index": 1,
            },
        }
    ]


def test_batch_reports_only_failed_message_ids(wiring):
    """成功と再試行しない失敗を含めず、再試行する失敗と例外のIDだけを入力順で返す。"""
    body = valid_body()
    messages = [
        {"messageId": "saved-before", "body": body},
        {"messageId": "failed-first", "body": body},
        {"messageId": "already", "body": body},
        {"messageId": "rejected", "body": body},
        {"messageId": "not-retried", "body": body},
        {"messageId": "retried", "body": body},
        {"messageId": "saved-after", "body": body},
        {"messageId": "failed-last", "body": body},
    ]
    wiring.consumer.consume.side_effect = [
        CurationCompletion(CurationCompletionKind.SIGNAL, 901),
        RuntimeError("first-failure"),
        CurationCompletion(CurationCompletionKind.ALREADY_CURATED),
        NoRetryCuration(
            CurationReadyBuildRejected(
                CurationReadyBuildRejectionReason.ARTICLE_MISSING
            )
        ),
        NoRetryCuration(
            AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
        ),
        RetryCuration(RuntimeError("decided-retry")),
        CurationCompletion(CurationCompletionKind.SIGNAL, 901),
        RuntimeError("last-failure"),
    ]

    response = module.handler({"Records": messages}, None)

    assert wiring.consumer.consume.await_count == 8
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "failed-first"},
            {"itemIdentifier": "retried"},
            {"itemIdentifier": "failed-last"},
        ]
    }


def test_each_payload_is_passed_to_the_shared_consumer(wiring):
    """各payloadとそのメッセージの相関情報を持つロガーをConsumerへ渡す。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(analyzable_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(analyzable_article_id=202),
        },
    ]

    module.handler({"Records": messages}, SimpleNamespace(aws_request_id="request-1"))

    wiring.open.assert_called_once_with(wiring.settings, logger=ANY)
    assert wiring.consumer.consume.await_args_list == [
        call(AnalyzableArticleCreated(analyzable_article_id=101), logger=ANY),
        call(AnalyzableArticleCreated(analyzable_article_id=202), logger=ANY),
    ]

    for invocation, message in zip(
        wiring.consumer.consume.await_args_list, messages, strict=True
    ):
        payload = invocation.args[0]
        context = structlog.get_context(invocation.kwargs["logger"])
        assert (
            context.items()
            >= {
                "service": "article_analysis",
                "stage": "curation",
                "environment": "test",
                "request_id": "request-1",
                "message_id": message["messageId"],
                "event_id": json.loads(message["body"])["event_id"],
                "analyzable_article_id": payload.analyzable_article_id,
            }.items()
        )


@pytest.mark.asyncio
async def test_messages_finish_sequentially_in_input_order(wiring):
    """先行メッセージの完了を待ってから、次のメッセージを処理する。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(analyzable_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(analyzable_article_id=202),
        },
    ]
    steps = []

    async def consume(payload, *, logger):
        steps.append(("start", payload.analyzable_article_id))
        await asyncio.sleep(0)
        steps.append(("end", payload.analyzable_article_id))
        return CurationCompletion(CurationCompletionKind.SIGNAL, 901)

    wiring.consumer.consume.side_effect = consume

    await module._run_curation(
        {"Records": messages}, wiring.settings, logger=wiring.log
    )

    assert steps == [("start", 101), ("end", 101), ("start", 202), ("end", 202)]


def test_empty_batch_completes_without_consumption(wiring):
    """空バッチはConsumerを実行せず、空の失敗一覧を返す。"""
    response = module.handler({"Records": []}, None)

    assert response == {"batchItemFailures": []}
    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings, logger=ANY)
    assert wiring.order == ["open", "close"]


@pytest.mark.parametrize(
    "invalid_message",
    [
        pytest.param({"messageId": "invalid"}, id="missing-body"),
        pytest.param({"messageId": "invalid", "body": "not-json"}, id="invalid-json"),
        pytest.param(
            {"messageId": "invalid", "body": valid_body(analyzable_article_id=0)},
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
        AnalyzableArticleCreated(analyzable_article_id=101), logger=ANY
    )


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


def test_unexpected_parser_failure_does_not_stop_batch(wiring, monkeypatch):
    """想定外の解析障害も、そのメッセージだけの失敗として後続を処理する。"""
    body = valid_body()
    parsed_body = SqsRecord(message_id="id", body=body).parse_json()
    parsed = module.AnalyzableArticleCreatedEvent.from_input(parsed_body)
    monkeypatch.setattr(
        SqsRecord,
        "parse_json",
        Mock(side_effect=[RuntimeError("private-parser"), parsed_body]),
    )
    messages = [
        {"messageId": "parse-failed", "body": body},
        {"messageId": "following", "body": body},
    ]

    response = module.handler({"Records": messages}, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "parse-failed"}]}
    wiring.consumer.consume.assert_awaited_once_with(parsed.payload, logger=ANY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_control_exception_stops_batch(wiring, read_logs, interruption):
    """キャンセル・終了要求は個別失敗に変換せず、終端を記録せずに後続処理を止めて伝播する。"""
    wiring.consumer.consume.side_effect = interruption
    body = valid_body()
    messages = [
        {"messageId": "interrupted", "body": body},
        {"messageId": "unprocessed", "body": body},
    ]

    with pytest.raises(type(interruption)) as caught:
        await module._run_curation(
            {"Records": messages},
            wiring.settings,
            logger=create_article_analysis_logger().bind(stage="curation"),
        )

    assert caught.value is interruption
    wiring.consumer.consume.assert_awaited_once()
    assert [log["event"] for log in read_logs()] == [
        "curation_message_processing_started"
    ]


def test_composition_failure_is_not_recorded_again(wiring, read_logs):
    """compositionが担当する初期化失敗を、入口で二重記録せず伝播する。"""
    original = RuntimeError("composition-failed")
    wiring.open.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler({"Records": []}, None)

    assert caught.value is original
    wiring.consumer.consume.assert_not_awaited()
    assert read_logs() == []


@pytest.mark.asyncio
async def test_processing_cancellation_leaves_borrowed_scope(wiring):
    """処理中キャンセルでも、Consumerの利用範囲を閉じて伝播する。"""
    wiring.consumer.consume.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await module._run_curation(
            {"Records": [{"messageId": "cancelled", "body": valid_body()}]},
            wiring.settings,
            logger=wiring.log,
        )

    assert wiring.order == ["open", "close"]


def test_shared_logging_context_is_scoped_to_invocation(wiring):
    """呼び出しの相関情報を資源の準備・解放まで共有し、呼び出し元へ持ち越さない。"""
    snapshots = {}

    @asynccontextmanager
    async def open_consumer(settings, *, logger):
        snapshots["initialization"] = structlog.contextvars.get_contextvars()
        try:
            yield wiring.consumer
        finally:
            snapshots["cleanup"] = structlog.contextvars.get_contextvars()

    wiring.open.side_effect = open_consumer
    with structlog.contextvars.bound_contextvars(request_id="outer-request"):
        outer_context = structlog.contextvars.get_contextvars()
        module.handler(
            {"Records": [{"messageId": "message-001", "body": valid_body()}]},
            SimpleNamespace(aws_request_id="request-001"),
        )
        assert structlog.contextvars.get_contextvars() == outer_context

    invocation = {
        "service": "article_analysis",
        "stage": "curation",
        "environment": "test",
        "request_id": "request-001",
    }
    assert snapshots["initialization"] == invocation
    assert snapshots["cleanup"] == invocation
