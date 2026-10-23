"""停止の記録の項目と設定修復のメトリクス条件、記録出力の境界を検証する(ログとEMFは標準出力のJSONから読み、handlerとの合成は実DB)。"""

import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.assessment.events import (
    AssessedEventInvalidError,
)
from app.analysis.curation.events import (
    CuratedEventInvalidError,
)
from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendCredentialsError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseField,
    SqsResponseInvalidError,
    SqsResponseInvalidReason,
)
from app.cloudwatch.emf import PIPELINE_NAMESPACE
from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
)
from app.collection.events import (
    AnalyzableEventInvalidError,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.outbox.delivery import failure_handler as handler_module
from app.outbox.delivery import failure_recording as recording
from app.outbox.delivery.failure_handler import (
    DeliveryStopped,
    DeliveryUpdateSkipped,
    OutboxDeliveryFailureHandler,
)
from app.outbox.delivery.repository import LeasedOutboxEvent
from app.outbox.delivery.retry_policy import NonRetryableReason
from app.outbox.publishing.contracts import EventEnvelope, PublishFailed
from app.outbox.publishing.route import EventRouteMismatchError
from app.shared.event_contract import (
    EventInvalid,
    EventInvalidReason,
)
from tests.cloudwatch.records import emf_records
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

PRIVATE = "PRIVATE_PAYLOAD_URL_CREDENTIAL"
STOP_REASON = NonRetryableReason.NON_RETRYABLE_FAILURE


def _missing_credentials():
    return MessageSendConfigurationError(
        reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
    )


def _sqs_rejection(reason, service_code):
    return SqsServiceError(reason=reason, service_code=service_code)


def _logs(out):
    """標準出力からEMFを除いたログを、実行ごとに変わる時刻と発生位置を除いて返す。"""
    logs = []
    for line in out.splitlines():
        log = json.loads(line)
        if "_aws" in log:
            continue
        del log["timestamp"]
        log.pop("frames", None)
        logs.append(log)
    return logs


class _BrokenOutput:
    """書き込みで指定の例外を送出する標準出力。"""

    def __init__(self, error):
        self._error = error

    def write(self, text):
        raise self._error

    def flush(self):
        raise self._error


@pytest.fixture
def leased_event():
    return LeasedOutboxEvent(
        envelope=EventEnvelope(
            event_id=uuid4(),
            event_type=PRIVATE,
            schema_version=1,
            occurred_at=PAST,
            payload={"secret": PRIVATE},
        ),
        lease_token=uuid4(),
        attempt_count=1,
    )


@pytest.mark.parametrize(
    ("error", "requires_fix", "fields"),
    [
        (
            _missing_credentials(),
            True,
            {
                "error_class": "app.aws.sqs.errors.MessageSendConfigurationError",
                "error_message": "メッセージの送信に必要な設定がありません",
                "error_details": {"reason": "missing_credentials"},
            },
        ),
        (
            MessageSendConfigurationError(
                reason=MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.MessageSendConfigurationError",
                "error_message": "メッセージの送信に必要な設定がありません",
                "error_details": {"reason": "incomplete_credentials"},
            },
        ),
        (
            MessageSendConfigurationError(
                reason=MessageSendConfigurationReason.MISSING_REGION
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.MessageSendConfigurationError",
                "error_message": "メッセージの送信に必要な設定がありません",
                "error_details": {"reason": "missing_region"},
            },
        ),
        (
            MessageSendCredentialsError(exception_type="CredentialRetrievalError"),
            False,
            {
                "error_class": "app.aws.sqs.errors.MessageSendCredentialsError",
                "error_message": "メッセージの送信に使う資格情報を取得できませんでした",
                "error_details": {"exception_type": "CredentialRetrievalError"},
            },
        ),
        (
            _sqs_rejection(
                SqsServiceErrorReason.AUTHENTICATION_FAILED, "InvalidClientTokenId"
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "authentication_failed",
                    "service_code": "InvalidClientTokenId",
                },
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.ACCESS_DENIED, "AccessDenied"),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "access_denied",
                    "service_code": "AccessDenied",
                },
            },
        ),
        (
            _sqs_rejection(
                SqsServiceErrorReason.DESTINATION_NOT_FOUND, "QueueDoesNotExist"
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "destination_not_found",
                    "service_code": "QueueDoesNotExist",
                },
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.THROTTLED, "RequestThrottled"),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "throttled",
                    "service_code": "RequestThrottled",
                },
            },
        ),
        (
            _sqs_rejection(
                SqsServiceErrorReason.SERVICE_UNAVAILABLE, "ServiceUnavailable"
            ),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "service_unavailable",
                    "service_code": "ServiceUnavailable",
                },
            },
        ),
        (
            _sqs_rejection(
                SqsServiceErrorReason.REQUEST_REJECTED, "InvalidParameterValue"
            ),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "request_rejected",
                    "service_code": "InvalidParameterValue",
                },
            },
        ),
        (
            _sqs_rejection(
                SqsServiceErrorReason.SECURITY_REJECTED, "IncompleteSignature"
            ),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "security_rejected",
                    "service_code": "IncompleteSignature",
                },
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.REQUEST_EXPIRED, "RequestExpired"),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "request_expired",
                    "service_code": "RequestExpired",
                },
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.ENCRYPTION_ERROR, "KmsDisabled"),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {
                    "reason": "encryption_error",
                    "service_code": "KmsDisabled",
                },
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.UNKNOWN, "FutureError"),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_message": "SQSがエラーを返し、メッセージを送信できませんでした",
                "error_details": {"reason": "unknown", "service_code": "FutureError"},
            },
        ),
        (
            MessageSendTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.UNKNOWN, HttpTransportFailureReason.TLS
                ),
                exception_type="SSLError",
            ),
            False,
            {
                "error_class": "app.aws.sqs.errors.MessageSendTransportError",
                "error_message": "メッセージの送信で通信が完了しませんでした",
                "error_details": {
                    "stage": "unknown",
                    "reason": "tls",
                    "proxy_status": None,
                    "exception_type": "SSLError",
                },
            },
        ),
        (
            MessageSendUnexpectedError(exception_type="ParamValidationError"),
            False,
            {
                "error_class": "app.aws.sqs.errors.MessageSendUnexpectedError",
                "error_message": "想定外の理由でメッセージを送信できませんでした",
                "error_details": {"exception_type": "ParamValidationError"},
            },
        ),
        (
            MessageTooLargeError(),
            False,
            {
                "error_class": "app.aws.sqs.outgoing_message.MessageTooLargeError",
                "error_message": "メッセージの本文が送信できる大きさを超えています",
            },
        ),
        (
            SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.MISSING,
                field=SqsResponseField.MESSAGE_ID,
            ),
            False,
            {
                "error_class": "app.aws.sqs.outgoing_message.SqsResponseInvalidError",
                "error_message": "SQSの応答を検証できませんでした",
                "error_details": {"reason": "missing", "field": "message_id"},
            },
        ),
        (
            MessageBodyMismatchError(),
            False,
            {
                "error_class": "app.aws.sqs.outgoing_message.MessageBodyMismatchError",
                "error_message": (
                    "SQSが受け取った本文が送信した本文と一致しません。"
                    "SQSには受け付けられている可能性があります"
                ),
            },
        ),
        (
            AnalyzableEventInvalidError(
                EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
            ),
            False,
            {
                "error_class": "app.collection.events.AnalyzableEventInvalidError",
                "error_message": "Validation failed: invalid_payload",
                "error_details": {
                    "kind": "application_validation",
                    "reason": "invalid_payload",
                    "issues": [],
                },
            },
        ),
        (
            IncompleteArticleEventInvalidError(
                EventInvalid(
                    reason=EventInvalidReason.INVALID_ENVELOPE,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": (
                    "app.collection.article_acquisition.events."
                    "IncompleteArticleEventInvalidError"
                ),
                "error_message": "Validation failed: invalid_envelope",
                "error_details": {
                    "kind": "application_validation",
                    "reason": "invalid_envelope",
                    "issues": [],
                },
            },
        ),
        (
            AssessedEventInvalidError(
                EventInvalid(
                    reason=EventInvalidReason.UNSUPPORTED_EVENT_TYPE,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": (
                    "app.analysis.assessment.events.AssessedEventInvalidError"
                ),
                "error_message": "Validation failed: unsupported_event_type",
                "error_details": {
                    "kind": "application_validation",
                    "reason": "unsupported_event_type",
                    "issues": [],
                },
            },
        ),
        (
            CuratedEventInvalidError(
                EventInvalid(
                    reason=EventInvalidReason.UNSUPPORTED_SCHEMA_VERSION,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": "app.analysis.curation.events.CuratedEventInvalidError",
                "error_message": "Validation failed: unsupported_schema_version",
                "error_details": {
                    "kind": "application_validation",
                    "reason": "unsupported_schema_version",
                    "issues": [],
                },
            },
        ),
        (
            EventRouteMismatchError(),
            False,
            {
                "error_class": "app.outbox.publishing.route.EventRouteMismatchError",
                "error_message": "イベントの種別が配送定義の種別と一致しません",
            },
        ),
    ],
)
def test_stop_log_fields_and_metric_by_failure(
    error, requires_fix, fields, leased_event, capsys
):
    """送り直しても直らない失敗の停止を、失敗の型・文・診断情報つきのerrorで記録し、設定修復の6理由だけメトリクスを出し、イベントの中身は出さない。"""
    recording.record_publish_failure(
        leased_event=leased_event, error=error, stop_reason=STOP_REASON
    )
    out = capsys.readouterr().out
    records = emf_records(out)
    assert _logs(out) == [
        {
            "event": "outbox_delivery_stopped",
            "level": "error",
            "log_policy": "pipeline_control",
            "event_id": str(leased_event.envelope.event_id),
            "attempt_count": 1,
            "stop_reason": "non_retryable_failure",
            "requires_configuration_fix": requires_fix,
            **fields,
        }
    ]
    assert len(records) == int(requires_fix)
    assert PRIVATE not in out
    if requires_fix:
        record = records[0]
        assert record[recording.CONFIGURATION_FAILURE_METRIC] == 1
        directive = record["_aws"]["CloudWatchMetrics"][0]
        assert directive == {
            "Namespace": "Vector/Pipeline",
            "Dimensions": [[]],
            "Metrics": [
                {"Name": recording.CONFIGURATION_FAILURE_METRIC, "Unit": "Count"}
            ],
        }
        assert set(record) == {"_aws", recording.CONFIGURATION_FAILURE_METRIC}


def test_retry_exhaustion_is_warning_and_not_alarm_target(leased_event, capsys):
    """送り直せる失敗で回数を使い切った停止はwarningで記録し、人の設定修復が必要という根拠にしない。"""
    recording.record_publish_failure(
        leased_event=leased_event,
        error=MessageSendTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
            exception_type="ReadTimeoutError",
        ),
        stop_reason=NonRetryableReason.RETRY_EXHAUSTED,
    )
    out = capsys.readouterr().out
    assert _logs(out) == [
        {
            "event": "outbox_delivery_stopped",
            "level": "warning",
            "log_policy": "pipeline_control",
            "event_id": str(leased_event.envelope.event_id),
            "attempt_count": 1,
            "stop_reason": "retry_exhausted",
            "requires_configuration_fix": False,
            "error_class": "app.aws.sqs.errors.MessageSendTransportError",
            "error_message": "メッセージの送信で通信が完了しませんでした",
            "error_details": {
                "stage": "receive",
                "reason": "timeout",
                "proxy_status": None,
                "exception_type": "ReadTimeoutError",
            },
        }
    ]
    assert emf_records(out) == []


@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
def test_output_failures_do_not_propagate_or_block_other_sink(
    failed_sink, leased_event, capsys, monkeypatch
):
    """ログの出力障害でもメトリクスを試み、メトリクスの出力障害は例外とともに記録し、どちらも配信処理へ戻さない。"""
    metric = Mock(
        side_effect=ValueError("metric sink failed")
        if failed_sink in ("metric", "both")
        else None
    )
    monkeypatch.setattr(recording, "emit_metric", metric)
    if failed_sink in ("log", "both"):
        monkeypatch.setattr(sys, "stdout", _BrokenOutput(OSError("log sink failed")))
    recording.record_publish_failure(
        leased_event=leased_event, error=_missing_credentials(), stop_reason=STOP_REASON
    )
    metric.assert_called_once()
    if failed_sink == "metric":
        logs = _logs(capsys.readouterr().out)
        assert [log["event"] for log in logs] == [
            "outbox_delivery_stopped",
            "outbox_failure_recording_failed",
        ]
        assert logs[1] == {
            "event": "outbox_failure_recording_failed",
            "level": "warning",
            "log_policy": "pipeline_control",
            "error_class": "builtins.ValueError",
            "error_message": "metric sink failed",
        }


@pytest.mark.parametrize("sink", ["log", "metric"])
def test_base_exception_is_not_swallowed(sink, leased_event, monkeypatch):
    """プロセス終了を記録の通常障害として握りつぶさない。"""
    failure = KeyboardInterrupt()
    if sink == "log":
        monkeypatch.setattr(sys, "stdout", _BrokenOutput(failure))
    else:
        monkeypatch.setattr(recording, "emit_metric", Mock(side_effect=failure))
    with pytest.raises(KeyboardInterrupt) as caught:
        recording.record_publish_failure(
            leased_event=leased_event,
            error=_missing_credentials(),
            stop_reason=STOP_REASON,
        )
    assert caught.value is failure


def test_alarm_watches_the_emitted_metric():
    """アラームがアプリの送る系列(namespace・名前・次元なし)を見ていることを確かめる。"""
    source = (
        Path(__file__).resolve().parents[4] / "infra/aws/news_pipeline_alerting.tf"
    ).read_text()
    match = re.search(
        r'resource "aws_cloudwatch_metric_alarm" '
        r'"outbox_publish_configuration_failure" \{(.*?)\n\}',
        source,
        re.S,
    )
    assert match is not None
    block = match.group(1)
    for key, value in {
        "namespace": json.dumps(PIPELINE_NAMESPACE),
        "metric_name": json.dumps(recording.CONFIGURATION_FAILURE_METRIC),
    }.items():
        assert re.search(rf"^\s*{key}\s*=\s*{re.escape(value)}\s*$", block, re.M)
    assert "dimensions" not in block


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["committed", "skipped", "commit_failed"])
@pytest.mark.parametrize(
    "failure_kind", ["configuration", "integrity", "response_invalid"]
)
async def test_handler_composition_records_only_committed_stops(
    session_factory,
    leased_event,
    mode,
    failure_kind,
    capsys,
    monkeypatch,
):
    """停止の可視性と、commit失敗・更新なしで記録しない境界を実DBで確認する。"""
    from dataclasses import replace

    async with session_factory() as seed:
        event_id = await insert_event(
            seed,
            next_attempt_at=PAST,
            lease_token=leased_event.lease_token,
            leased_until=FUTURE,
            attempt_count=1,
        )
        await seed.commit()
    leased_event = replace(
        leased_event, envelope=replace(leased_event.envelope, event_id=event_id)
    )
    if mode == "skipped":
        leased_event = replace(leased_event, lease_token=uuid4())

    def fail_commit(session):
        raise SQLAlchemyError("injected commit failure")

    session_exited = False
    saved_after_exit = None

    @asynccontextmanager
    async def factory():
        nonlocal session_exited, saved_after_exit
        async with session_factory() as session:
            if mode == "commit_failed":
                sqlalchemy_event.listen(
                    session.sync_session, "before_commit", fail_commit
                )
            yield session
        session_exited = True
        async with session_factory() as reader:
            saved_after_exit = await read_event(reader, event_id)

    def record_after_exit(**kwargs):
        assert session_exited
        assert saved_after_exit["delivery_stopped_at"] is not None
        recording.record_publish_failure(**kwargs)

    record = Mock(side_effect=record_after_exit)
    monkeypatch.setattr(handler_module, "record_publish_failure", record)

    async def handle_failure():
        if failure_kind == "integrity":
            error = MessageBodyMismatchError()
        elif failure_kind == "response_invalid":
            error = SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.MISSING,
                field=SqsResponseField.MESSAGE_ID,
            )
        else:
            error = _missing_credentials()
        result = await OutboxDeliveryFailureHandler(factory, jitter=lambda: 0.5).handle(
            leased_event=leased_event,
            failure=PublishFailed(leased_event.envelope.event_id, error),
        )
        async with session_factory() as reader:
            saved = await read_event(reader, event_id)
        assert (saved["delivery_stopped_at"] is not None) == (mode == "committed")
        assert result == (
            DeliveryStopped(STOP_REASON)
            if mode == "committed"
            else DeliveryUpdateSkipped()
        )

    if mode == "commit_failed":
        with pytest.raises(SQLAlchemyError):
            await handle_failure()
    else:
        await handle_failure()
    out = capsys.readouterr().out
    stops = [log for log in _logs(out) if log["event"] == "outbox_delivery_stopped"]
    assert record.call_count == int(mode == "committed")
    assert len(stops) == int(mode == "committed")
    assert len(emf_records(out)) == int(
        mode == "committed" and failure_kind == "configuration"
    )
    if failure_kind == "integrity" and mode == "committed":
        assert (stops[0]["error_class"], stops[0]["error_message"]) == (
            "app.aws.sqs.outgoing_message.MessageBodyMismatchError",
            "SQSが受け取った本文が送信した本文と一致しません。"
            "SQSには受け付けられている可能性があります",
        )
    if failure_kind == "response_invalid" and mode == "committed":
        assert (
            stops[0]["error_details"],
            stops[0]["requires_configuration_fix"],
        ) == ({"reason": "missing", "field": "message_id"}, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
async def test_handler_returns_committed_stop_despite_output_failure(
    session_factory, leased_event, failed_sink, capsys, monkeypatch
):
    """記録先の障害でも停止の確定結果を維持し、再処理で記録を繰り返さない。"""
    from dataclasses import replace

    async with session_factory() as seed:
        event_id = await insert_event(
            seed,
            next_attempt_at=PAST,
            lease_token=leased_event.lease_token,
            leased_until=FUTURE,
            attempt_count=1,
        )
        await seed.commit()
    leased_event = replace(
        leased_event, envelope=replace(leased_event.envelope, event_id=event_id)
    )
    metric = Mock(
        side_effect=RuntimeError("metric sink failed")
        if failed_sink in ("metric", "both")
        else None
    )
    monkeypatch.setattr(recording, "emit_metric", metric)
    if failed_sink in ("log", "both"):
        monkeypatch.setattr(sys, "stdout", _BrokenOutput(OSError("log sink failed")))
    handler = OutboxDeliveryFailureHandler(session_factory, jitter=lambda: 0.5)
    failure = PublishFailed(event_id, _missing_credentials())
    assert await handler.handle(
        leased_event=leased_event, failure=failure
    ) == DeliveryStopped(STOP_REASON)
    async with session_factory() as reader:
        saved = await read_event(reader, event_id)
    assert saved["delivery_stop_reason"] == STOP_REASON.value
    assert saved["delivery_stopped_at"] is not None
    assert saved["lease_token"] is None
    assert (
        await handler.handle(leased_event=leased_event, failure=failure)
        == DeliveryUpdateSkipped()
    )
    metric.assert_called_once()
    if failed_sink == "metric":
        assert [log["event"] for log in _logs(capsys.readouterr().out)] == [
            "outbox_delivery_stopped",
            "outbox_failure_recording_failed",
        ]
    async with session_factory() as reader:
        assert await read_event(reader, event_id) == saved
