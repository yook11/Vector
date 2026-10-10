"""Assessment入口の入力の受け渡し・失敗範囲・処理順を確認する。"""

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
from app.analysis.assessment.consumer_failure_classification import (
    NoRetryAssessment,
    RetryAssessment,
)
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    AssessmentReadyBuildRejectionReason,
)
from app.analysis.assessment.errors import AssessmentCurationMissingError
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import ArticleCuratedSignal
from app.analysis.logging import create_article_analysis_logger
from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason
from app.lambda_handlers.sqs.received_message import ReceivedMessage

module = import_module("app.lambda_handlers.assessment.handler")
pytestmark = pytest.mark.unit


def valid_body(*, curation_id=11, analyzable_article_id=101):
    return json.dumps(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "article.curated_signal",
            "schema_version": 1,
            "occurred_at": "2026-09-12T00:00:00Z",
            "payload": {
                "curation_id": curation_id,
                "analyzable_article_id": analyzable_article_id,
            },
        }
    )


@pytest.fixture
def wiring(monkeypatch):
    state = SimpleNamespace(
        order=[],
        settings=SimpleNamespace(aws_region="ap-northeast-1", env="test"),
        notification_settings=object(),
        consumer=SimpleNamespace(
            consume=AsyncMock(
                return_value=AssessmentCompletion(
                    AssessmentCompletionKind.IN_SCOPE, 901
                )
            )
        ),
        log=create_article_analysis_logger(),
        failure_recorder=Mock(),
    )

    @asynccontextmanager
    async def open_consumer(settings, *, failure_recorder):
        state.order.append("open")
        try:
            yield state.consumer
        finally:
            state.order.append("close")

    state.open = Mock(side_effect=open_consumer)
    monkeypatch.setattr(module, "open_assessment_consumer", state.open)
    state.settings_factory = Mock(
        return_value=SimpleNamespace(
            consumer=state.settings,
            notification=state.notification_settings,
            env=state.settings.env,
        )
    )
    monkeypatch.setattr(
        module, "AssessmentLambdaSettings", SimpleNamespace(load=state.settings_factory)
    )
    state.notifier = SimpleNamespace(notify_article_list_updated=AsyncMock())
    state.build_notifier = Mock(return_value=state.notifier)
    monkeypatch.setattr(module, "build_article_list_notifier", state.build_notifier)
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
                SimpleNamespace(aws_request_id="request-001"),
            )

            configure_spy.assert_not_called()
            assert structlog.get_config() == global_config

        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        completed = next(
            record
            for record in records
            if record["event"] == "assessment_message_processing_completed"
        )
        assert completed["log_policy"] == "ai_inference"
        assert completed["message_id"] == "message-001"
    finally:
        configure(**original_config)


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
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 901),
        RuntimeError("first-failure"),
        AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED),
        NoRetryAssessment(
            AssessmentReadyBuildRejected(
                AssessmentReadyBuildRejectionReason.CURATION_MISSING
            )
        ),
        NoRetryAssessment(
            AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
        ),
        RetryAssessment(RuntimeError("decided-retry")),
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 901),
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


@pytest.mark.parametrize(
    ("cause", "classification"),
    [
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED),
            {"code": "ai_provider_result_error", "failure_reason": "input_blocked"},
            id="ai_input_blocked",
        ),
        pytest.param(
            AssessmentCurationMissingError(),
            {"code": "assessment_curation_missing"},
            id="curation_missing",
        ),
    ],
)
def test_failure_not_retried_is_completed_with_its_classification(
    wiring, capsys, cause, classification
):
    """再試行しない失敗は失敗一覧に含めず、codeと、AIの失敗なら理由を記録する。"""
    wiring.consumer.consume.return_value = NoRetryAssessment(cause)

    response = module.handler(
        {"Records": [{"messageId": "not-retried", "body": valid_body()}]}, None
    )

    assert response == {"batchItemFailures": []}
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failed = next(
        record
        for record in records
        if record["event"] == "assessment_message_processing_failed"
    )
    assert {
        key: failed[key] for key in ("code", "failure_reason") if key in failed
    } == classification
    assert failed["message_disposition"] == "completed"


def test_retry_decision_is_reported_like_a_processing_failure(wiring, capsys):
    """Consumerが再試行と決めた失敗は、例外と同じ記録で失敗一覧に載せる。"""
    wiring.consumer.consume.return_value = RetryAssessment(
        RuntimeError("private-exception")
    )

    response = module.handler(
        {"Records": [{"messageId": "retried", "body": valid_body()}]}, None
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "retried"}]}
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failed = next(
        record
        for record in records
        if record["event"] == "assessment_message_processing_failed"
    )
    assert failed["level"] == "error"
    assert failed["message_disposition"] == "batch_item_failure"
    assert "code" not in failed


def test_each_payload_is_passed_to_the_shared_consumer(wiring):
    """各payloadを、そのメッセージの相関情報だけを置いた文脈でConsumerへ渡す。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(curation_id=11, analyzable_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(curation_id=22, analyzable_article_id=202),
        },
    ]
    contexts = []

    async def consume(payload, *, logger):
        contexts.append(structlog.contextvars.get_contextvars())
        return AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE)

    wiring.consumer.consume.side_effect = consume

    module.handler({"Records": messages}, SimpleNamespace(aws_request_id="request-1"))

    wiring.open.assert_called_once_with(wiring.settings, failure_recorder=ANY)
    wiring.build_notifier.assert_called_once_with(
        wiring.notification_settings, aws_region="ap-northeast-1"
    )
    assert wiring.consumer.consume.await_args_list == [
        call(
            ArticleCuratedSignal(curation_id=11, analyzable_article_id=101), logger=ANY
        ),
        call(
            ArticleCuratedSignal(curation_id=22, analyzable_article_id=202), logger=ANY
        ),
    ]
    assert contexts == [
        {
            "service": "article_analysis",
            "stage": "assessment",
            "environment": "test",
            "request_id": "request-1",
            "message_id": "first",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "curation_id": 11,
            "analyzable_article_id": 101,
        },
        {
            "service": "article_analysis",
            "stage": "assessment",
            "environment": "test",
            "request_id": "request-1",
            "message_id": "second",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "curation_id": 22,
            "analyzable_article_id": 202,
        },
    ]


@pytest.mark.asyncio
async def test_messages_finish_sequentially_in_input_order(wiring):
    """先行メッセージの完了を待ってから、次のメッセージを処理する。"""
    messages = [
        {
            "messageId": "first",
            "body": valid_body(curation_id=11, analyzable_article_id=101),
        },
        {
            "messageId": "second",
            "body": valid_body(curation_id=22, analyzable_article_id=202),
        },
    ]
    steps = []

    async def consume(payload, *, logger):
        steps.append(("start", payload.curation_id))
        await asyncio.sleep(0)
        steps.append(("end", payload.curation_id))
        return AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 901)

    wiring.consumer.consume.side_effect = consume

    await module._run_assessment(
        {"Records": messages},
        wiring.settings,
        wiring.notifier,
        logger=wiring.log,
        failure_recorder=wiring.failure_recorder,
    )

    assert steps == [("start", 11), ("end", 11), ("start", 22), ("end", 22)]


def test_empty_batch_completes_without_consumption(wiring):
    """空バッチはConsumerを実行せず、空の失敗一覧を返す。"""
    response = module.handler({"Records": []}, None)

    assert response == {"batchItemFailures": []}
    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings, failure_recorder=ANY)
    assert wiring.order == ["open", "close"]


def test_invalid_message_id_rejects_batch_before_consumption(wiring):
    """後続のIDが欠けていたら、先行分も処理せずバッチ全体を失敗にする。"""
    body = valid_body()
    messages = [{"messageId": "valid", "body": body}, {"body": body}]

    with pytest.raises(SqsInputError):
        module.handler({"Records": messages}, None)

    wiring.consumer.consume.assert_not_awaited()
    wiring.open.assert_called_once_with(wiring.settings, failure_recorder=ANY)
    assert wiring.order == ["open", "close"]


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
        ArticleCuratedSignal(curation_id=11, analyzable_article_id=101), logger=ANY
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


@pytest.mark.parametrize(
    "completion",
    [
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 901),
        AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE),
        AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED),
    ],
)
def test_completion_is_not_reported_as_batch_failure(wiring, completion):
    """Consumerの正常完了は種類にかかわらず失敗一覧に含めない。"""
    wiring.consumer.consume.return_value = completion

    response = module.handler(
        {"Records": [{"messageId": "completed", "body": valid_body()}]}, None
    )

    assert response == {"batchItemFailures": []}


def test_unexpected_parser_failure_does_not_stop_batch(wiring, monkeypatch):
    """想定外の解析障害も、そのメッセージだけの失敗として後続を処理する。"""
    body = valid_body()
    parsed_body = ReceivedMessage(message_id="id", body=body).parse_json()
    parsed = module.ArticleCuratedSignalEvent.from_input(parsed_body)
    monkeypatch.setattr(
        ReceivedMessage,
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
async def test_control_exception_stops_batch(wiring, interruption):
    """キャンセル・終了要求は個別失敗に変換せず、後続処理を止めて伝播する。"""
    wiring.consumer.consume.side_effect = interruption
    body = valid_body()
    messages = [
        {"messageId": "interrupted", "body": body},
        {"messageId": "unprocessed", "body": body},
    ]

    with pytest.raises(type(interruption)) as caught:
        await module._run_assessment(
            {"Records": messages},
            wiring.settings,
            wiring.notifier,
            logger=wiring.log,
            failure_recorder=wiring.failure_recorder,
        )

    assert caught.value is interruption
    wiring.consumer.consume.assert_awaited_once()


def test_settings_failure_is_logged_and_aborts_entire_batch(wiring, read_logs):
    """設定（Consumerと通知）の読み込みの失敗は個別応答にせず、settingsの処理として記録して元の例外を伝える。"""
    original = RuntimeError("private-settings")
    wiring.settings_factory.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler(
            {"Records": [{"messageId": "unprocessed", "body": valid_body()}]},
            SimpleNamespace(aws_request_id="request-001"),
        )

    assert caught.value is original
    wiring.open.assert_not_called()
    wiring.build_notifier.assert_not_called()
    assert read_logs() == [
        {
            "event": "assessment_initialization_failed",
            "level": "error",
            "log_policy": "ai_inference",
            "service": "article_analysis",
            "stage": "assessment",
            "request_id": "request-001",
            "operation": "settings",
            "error_class": "builtins.RuntimeError",
            "error_message": "private-settings",
        }
    ]


def test_composition_failure_preserves_original_exception(wiring):
    """compositionの初期化失敗ではConsumerを呼ばず元の例外を伝える。"""
    original = RuntimeError("composition-failed")
    wiring.open.side_effect = original

    with pytest.raises(RuntimeError) as caught:
        module.handler({"Records": []}, None)

    assert caught.value is original
    wiring.consumer.consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_cancellation_leaves_borrowed_scope(wiring):
    """処理中キャンセルでも、Consumerの利用範囲を閉じて伝播する。"""
    wiring.consumer.consume.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await module._run_assessment(
            {"Records": [{"messageId": "cancelled", "body": valid_body()}]},
            wiring.settings,
            wiring.notifier,
            logger=wiring.log,
            failure_recorder=wiring.failure_recorder,
        )

    assert wiring.order == ["open", "close"]


@pytest.mark.parametrize(
    "reason",
    [
        AssessmentReadyBuildRejectionReason.CURATION_MISSING,
        AssessmentReadyBuildRejectionReason.INPUT_INVALID,
    ],
)
def test_ready_build_rejection_is_not_reported_as_batch_failure(wiring, reason):
    """前提不成立は再処理を要求せず、失敗一覧に含めない。"""
    wiring.consumer.consume.return_value = NoRetryAssessment(
        AssessmentReadyBuildRejected(reason)
    )
    response = module.handler(
        {"Records": [{"messageId": "rejected", "body": valid_body()}]}, None
    )
    assert response == {"batchItemFailures": []}


def test_shared_logging_context_is_scoped_to_invocation_and_notification(wiring):
    """メッセージの相関情報を通知まで共有し、cleanupと呼び出し元へ持ち越さない。"""
    snapshots = {}

    @asynccontextmanager
    async def open_consumer(settings, *, failure_recorder):
        snapshots["initialization"] = structlog.contextvars.get_contextvars()
        try:
            yield wiring.consumer
        finally:
            snapshots["cleanup"] = structlog.contextvars.get_contextvars()

    async def notify():
        snapshots["notification"] = structlog.contextvars.get_contextvars()

    wiring.open.side_effect = open_consumer
    wiring.notifier.notify_article_list_updated.side_effect = notify
    with structlog.contextvars.bound_contextvars(request_id="outer-request"):
        outer_context = structlog.contextvars.get_contextvars()
        module.handler(
            {"Records": [{"messageId": "message-001", "body": valid_body()}]},
            SimpleNamespace(aws_request_id="request-001"),
        )
        assert structlog.contextvars.get_contextvars() == outer_context

    invocation = {
        "service": "article_analysis",
        "stage": "assessment",
        "environment": "test",
        "request_id": "request-001",
    }
    assert snapshots["initialization"] == invocation
    assert snapshots["cleanup"] == invocation
    assert snapshots["notification"] == {
        **invocation,
        "message_id": "message-001",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "curation_id": 11,
        "analyzable_article_id": 101,
    }
