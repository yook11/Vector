"""停止の記録の項目と設定修復のメトリクス条件、記録出力の境界を検証する(ログはstructlogで捕捉し、EMFは標準出力から読み、handlerとの合成は実DB)。"""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

from app.analysis.assessment.events import (
    AssessedEventInvalid,
    AssessedEventInvalidError,
    AssessedEventInvalidReason,
)
from app.analysis.curation.events import (
    CuratedEventInvalid,
    CuratedEventInvalidError,
    CuratedEventInvalidReason,
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
    IncompleteArticleEventInvalid,
    IncompleteArticleEventInvalidError,
    IncompleteArticleEventInvalidReason,
)
from app.collection.events import (
    AnalyzableEventInvalid,
    AnalyzableEventInvalidError,
    AnalyzableEventInvalidReason,
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
from tests.cloudwatch.records import emf_records
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

PRIVATE = "PRIVATE_PAYLOAD_URL_CREDENTIAL"
STOP_REASON = NonRetryableReason.NON_RETRYABLE_FAILURE


def _missing_credentials():
    return MessageSendConfigurationError(
        reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
    )


def _sqs_rejection(reason):
    return SqsServiceError(reason=reason, service_code=PRIVATE)


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
                "error_reason": "missing_credentials",
            },
        ),
        (
            MessageSendConfigurationError(
                reason=MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.MessageSendConfigurationError",
                "error_reason": "incomplete_credentials",
            },
        ),
        (
            MessageSendConfigurationError(
                reason=MessageSendConfigurationReason.MISSING_REGION
            ),
            True,
            {
                "error_class": "app.aws.sqs.errors.MessageSendConfigurationError",
                "error_reason": "missing_region",
            },
        ),
        (
            MessageSendCredentialsError(exception_type="CredentialRetrievalError"),
            False,
            {
                "error_class": "app.aws.sqs.errors.MessageSendCredentialsError",
                "exception_type": "CredentialRetrievalError",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.AUTHENTICATION_FAILED),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "authentication_failed",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.ACCESS_DENIED),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "access_denied",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.DESTINATION_NOT_FOUND),
            True,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "destination_not_found",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.THROTTLED),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "throttled",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.SERVICE_UNAVAILABLE),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "service_unavailable",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.REQUEST_REJECTED),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "request_rejected",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.SECURITY_REJECTED),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "security_rejected",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.REQUEST_EXPIRED),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "request_expired",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.ENCRYPTION_ERROR),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "encryption_error",
            },
        ),
        (
            _sqs_rejection(SqsServiceErrorReason.UNKNOWN),
            False,
            {
                "error_class": "app.aws.sqs.errors.SqsServiceError",
                "error_reason": "unknown",
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
                "transport_stage": "unknown",
                "transport_reason": "tls",
            },
        ),
        (
            MessageSendUnexpectedError(exception_type="ParamValidationError"),
            False,
            {
                "error_class": "app.aws.sqs.errors.MessageSendUnexpectedError",
                "exception_type": "ParamValidationError",
            },
        ),
        (
            MessageTooLargeError(),
            False,
            {"error_class": "app.aws.sqs.outgoing_message.MessageTooLargeError"},
        ),
        (
            SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.MISSING,
                field=SqsResponseField.MESSAGE_ID,
            ),
            False,
            {"error_class": "app.aws.sqs.outgoing_message.SqsResponseInvalidError"},
        ),
        (
            MessageBodyMismatchError(),
            False,
            {
                "error_class": "app.aws.sqs.outgoing_message.MessageBodyMismatchError",
                "error_message": (
                    "送信した本文と、送信先が受け取った本文のチェックサムが一致しません。"
                    "送信先には受付済みの可能性があるため、このイベントの自動再試行を停止しました。"
                ),
            },
        ),
        (
            AnalyzableEventInvalidError(
                AnalyzableEventInvalid(
                    reason=AnalyzableEventInvalidReason.INVALID_PAYLOAD, issues=()
                )
            ),
            False,
            {
                "error_class": "app.collection.events.AnalyzableEventInvalidError",
                "error_reason": "invalid_payload",
            },
        ),
        (
            IncompleteArticleEventInvalidError(
                IncompleteArticleEventInvalid(
                    reason=IncompleteArticleEventInvalidReason.INVALID_ENVELOPE,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": (
                    "app.collection.article_acquisition.events."
                    "IncompleteArticleEventInvalidError"
                ),
                "error_reason": "invalid_envelope",
            },
        ),
        (
            AssessedEventInvalidError(
                AssessedEventInvalid(
                    reason=AssessedEventInvalidReason.UNSUPPORTED_EVENT_TYPE,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": (
                    "app.analysis.assessment.events.AssessedEventInvalidError"
                ),
                "error_reason": "unsupported_event_type",
            },
        ),
        (
            CuratedEventInvalidError(
                CuratedEventInvalid(
                    reason=CuratedEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION,
                    issues=(),
                )
            ),
            False,
            {
                "error_class": "app.analysis.curation.events.CuratedEventInvalidError",
                "error_reason": "unsupported_schema_version",
            },
        ),
        (
            EventRouteMismatchError(),
            False,
            {"error_class": "app.outbox.publishing.route.EventRouteMismatchError"},
        ),
    ],
)
def test_stop_log_fields_and_metric_by_failure(
    error, requires_fix, fields, leased_event, capsys
):
    """停止を失敗ごとに決まった項目で記録し、設定修復の6理由だけメトリクスを出し、SQSの生のコード・自由文・元例外は出さない。"""
    error.__cause__ = RuntimeError(PRIVATE)
    assert recording.requires_publish_configuration_fix(error) is requires_fix
    with capture_logs() as logs:
        recording.record_publish_failure(
            leased_event=leased_event, error=error, stop_reason=STOP_REASON
        )
    records = emf_records(capsys.readouterr().out)
    assert logs == [
        {
            "event": "outbox_delivery_stopped",
            "log_level": "info",
            "event_id": str(leased_event.envelope.event_id),
            "attempt_count": 1,
            "stop_reason": "non_retryable_failure",
            "requires_configuration_fix": requires_fix,
            **fields,
        }
    ]
    assert len(records) == int(requires_fix)
    assert PRIVATE not in json.dumps([logs, records])
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


def test_retry_exhaustion_alone_is_not_alarm_target(leased_event, capsys):
    """再試行上限は人の設定修復が必要という根拠にしない。"""
    with capture_logs() as logs:
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
    assert len(logs) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
def test_output_failures_do_not_propagate_or_block_other_sink(
    failed_sink,
    leased_event,
    monkeypatch,
):
    """出力障害とその記録障害でも再帰せず、独立した出力を試みる。"""
    logger = Mock()
    metric = Mock()
    logger.warning.side_effect = RuntimeError(PRIVATE)
    if failed_sink in ("log", "both"):
        logger.info.side_effect = RuntimeError(PRIVATE)
    if failed_sink in ("metric", "both"):
        metric.side_effect = ValueError(PRIVATE)
    monkeypatch.setattr(recording, "logger", logger)
    monkeypatch.setattr(recording, "emit_metric", metric)
    recording.record_publish_failure(
        leased_event=leased_event, error=_missing_credentials(), stop_reason=STOP_REASON
    )
    logger.info.assert_called_once()
    metric.assert_called_once()
    assert logger.warning.call_count == (2 if failed_sink == "both" else 1)
    assert PRIVATE not in str(logger.warning.call_args_list)


@pytest.mark.parametrize("sink", ["log", "metric"])
def test_base_exception_is_not_swallowed(sink, leased_event, monkeypatch):
    """プロセス終了を記録の通常障害として握りつぶさない。"""
    failure = KeyboardInterrupt()
    if sink == "log":
        logger = Mock()
        logger.info.side_effect = failure
        monkeypatch.setattr(recording, "logger", logger)
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

    with capture_logs() as logs:
        if mode == "commit_failed":
            with pytest.raises(SQLAlchemyError):
                await handle_failure()
        else:
            await handle_failure()
    assert record.call_count == int(mode == "committed")
    assert len(
        [log for log in logs if log["event"] == "outbox_delivery_stopped"]
    ) == int(mode == "committed")
    assert len(emf_records(capsys.readouterr().out)) == int(
        mode == "committed" and failure_kind == "configuration"
    )
    if failure_kind == "integrity" and mode == "committed":
        stop = next(log for log in logs if log["event"] == "outbox_delivery_stopped")
        assert stop["error_class"] == (
            "app.aws.sqs.outgoing_message.MessageBodyMismatchError"
        )
        assert "自動再試行を停止しました" in stop["error_message"]
    if failure_kind == "response_invalid" and mode == "committed":
        stop = next(log for log in logs if log["event"] == "outbox_delivery_stopped")
        assert "error_reason" not in stop
        assert "response_field" not in stop
        assert not stop["requires_configuration_fix"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
async def test_handler_returns_committed_stop_despite_output_failure(
    session_factory, leased_event, failed_sink, monkeypatch
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
    logger = Mock()
    metric = Mock()
    logger.warning.side_effect = RuntimeError(PRIVATE)
    if failed_sink in ("log", "both"):
        logger.info.side_effect = RuntimeError(PRIVATE)
    if failed_sink in ("metric", "both"):
        metric.side_effect = RuntimeError(PRIVATE)
    monkeypatch.setattr(recording, "logger", logger)
    monkeypatch.setattr(recording, "emit_metric", metric)
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
    logger.info.assert_called_once()
    metric.assert_called_once()
    async with session_factory() as reader:
        assert await read_event(reader, event_id) == saved
