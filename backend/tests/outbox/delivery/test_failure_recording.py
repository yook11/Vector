"""設定修復のメトリクス条件と記録出力の境界を検証する。"""

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

from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox.delivery import failure_handler as handler_module
from app.outbox.delivery import failure_recording as recording
from app.outbox.delivery.failure_handler import (
    DeliveryStopped,
    DeliveryUpdateSkipped,
    OutboxDeliveryFailureHandler,
)
from app.outbox.delivery.repository import ClaimedOutboxEvent
from app.outbox.delivery.retry_policy import NonRetryableReason
from app.outbox.publishing.errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishIntegrityError,
    PublishIntegrityReason,
    PublishPhase,
    PublishResponseInvalidError,
    PublishResponseInvalidReason,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.publishing.publisher import PublishFailed
from tests.cloudwatch.records import emf_records
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

PRIVATE = "PRIVATE_PAYLOAD_URL_CREDENTIAL"
STOP_REASON = NonRetryableReason.NON_RETRYABLE_FAILURE
CASES = [
    *[
        (
            PublishConfigurationError(reason=reason),
            reason
            in {
                PublishConfigurationReason.MISSING_CREDENTIALS,
                PublishConfigurationReason.INCOMPLETE_CREDENTIALS,
                PublishConfigurationReason.MISSING_REGION,
            },
        )
        for reason in PublishConfigurationReason
    ],
    *[
        (
            PublishServiceError(
                reason=reason,
                service_error_code=PRIVATE,
                status_code=403,
                request_id=PRIVATE,
            ),
            reason
            in {
                PublishServiceReason.AUTHENTICATION_FAILED,
                PublishServiceReason.ACCESS_DENIED,
                PublishServiceReason.DESTINATION_NOT_FOUND,
            },
        )
        for reason in PublishServiceReason
    ],
    *[
        (PublishEventInvalidError(reason=reason), False)
        for reason in PublishEventInvalidReason
    ],
    *[
        (PublishTransportError(failure=HttpTransportFailure(kind, True)), False)
        for kind in HttpTransportFailureKind
    ],
    *[
        (
            PublishUnexpectedError(
                original_exception=RuntimeError(PRIVATE),
                phase=phase,
                classification_exception=ValueError(PRIVATE),
            ),
            False,
        )
        for phase in PublishPhase
    ],
    *[
        (
            PublishResponseInvalidError(reason=reason),
            False,
        )
        for reason in PublishResponseInvalidReason
    ],
    (PublishError(), False),
    (
        PublishIntegrityError(
            reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH, request_id=PRIVATE
        ),
        False,
    ),
]


@pytest.fixture
def claimed():
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type=PRIVATE,
        schema_version=1,
        occurred_at=PAST,
        payload={"secret": PRIVATE},
        attempt_count=1,
        lease_token=uuid4(),
        leased_until=FUTURE,
    )


@pytest.mark.parametrize(("error", "expected"), CASES)
def test_all_reasons_log_stops_and_only_six_reasons_emit_metric(
    error,
    expected,
    claimed,
    capsys,
):
    """メトリクス対象だけ打点し、ログやEMFへ自由文と元例外を出さない。"""
    error.__cause__ = RuntimeError(PRIVATE)
    assert recording.requires_publish_configuration_fix(error) is expected
    with capture_logs() as logs:
        recording.record_publish_failure(
            event=claimed, error=error, stop_reason=STOP_REASON
        )
    records = emf_records(capsys.readouterr().out)
    assert len(records) == int(expected)
    assert len(logs) == 1
    log = logs[0]
    assert log["event"] == "outbox_delivery_stopped"
    assert log["event_id"] == str(claimed.event_id)
    assert log["attempt_count"] == claimed.attempt_count
    assert log["stop_reason"] == STOP_REASON.value
    assert log["error_code"] == error.CODE
    assert log["requires_configuration_fix"] is expected
    if isinstance(error, PublishTransportError):
        assert log["transport_kind"] == error.failure.kind.value
    elif isinstance(error, PublishResponseInvalidError):
        assert log["error_reason"] == error.reason.value
        assert "response_field" not in log
        assert "original_exception_type" not in log
    elif isinstance(error, PublishUnexpectedError):
        assert log["phase"] == error.phase.value
        assert log["original_exception_type"] == "builtins.RuntimeError"
        assert log["classification_exception_type"] == "builtins.ValueError"
    elif isinstance(
        error,
        (
            PublishServiceError,
            PublishConfigurationError,
            PublishEventInvalidError,
            PublishIntegrityError,
        ),
    ):
        assert log["error_reason"] == error.reason.value
    assert PRIVATE not in json.dumps([logs, records])
    if expected:
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


def test_retry_exhaustion_alone_is_not_alarm_target(claimed, capsys):
    """再試行上限は人の設定修復が必要という根拠にしない。"""
    with capture_logs() as logs:
        recording.record_publish_failure(
            event=claimed,
            error=PublishTransportError(
                failure=HttpTransportFailure(
                    HttpTransportFailureKind.READ_TIMEOUT, True
                )
            ),
            stop_reason=NonRetryableReason.RETRY_EXHAUSTED,
        )
    assert len(logs) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
def test_output_failures_do_not_propagate_or_block_other_sink(
    failed_sink,
    claimed,
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
        event=claimed, error=CASES[0][0], stop_reason=STOP_REASON
    )
    logger.info.assert_called_once()
    metric.assert_called_once()
    assert logger.warning.call_count == (2 if failed_sink == "both" else 1)
    assert PRIVATE not in str(logger.warning.call_args_list)


@pytest.mark.parametrize("sink", ["log", "metric"])
def test_base_exception_is_not_swallowed(sink, claimed, monkeypatch):
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
            event=claimed, error=CASES[0][0], stop_reason=STOP_REASON
        )
    assert caught.value is failure


def test_alarm_matches_emitted_metric_and_only_notifies_alarm_state():
    """Terraformとアプリの系列名・評価条件・通知経路の一致を固定する。"""
    source = (Path(__file__).resolve().parents[4] / "infra/aws/alerting.tf").read_text()
    match = re.search(
        r'resource "aws_cloudwatch_metric_alarm" '
        r'"outbox_publish_configuration_failure" \{(.*?)\n\}',
        source,
        re.S,
    )
    assert match is not None
    block = match.group(1)
    for key, value in {
        "namespace": '"Vector/Pipeline"',
        "metric_name": json.dumps(recording.CONFIGURATION_FAILURE_METRIC),
        "statistic": '"Sum"',
        "period": "60",
        "threshold": "1",
        "comparison_operator": '"GreaterThanOrEqualToThreshold"',
        "evaluation_periods": "1",
        "datapoints_to_alarm": "1",
        "treat_missing_data": '"notBreaching"',
        "alarm_actions": "[aws_sns_topic.alerts.arn]",
    }.items():
        assert re.search(rf"^\s*{key}\s*=\s*{re.escape(value)}\s*$", block, re.M)
    assert "ok_actions" not in block
    assert "insufficient_data_actions" not in block
    assert "dimensions" not in block
    assert "aws_cloudwatch_log_group.outbox_relay.name" in block


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["committed", "skipped", "commit_failed"])
@pytest.mark.parametrize(
    "failure_kind", ["configuration", "integrity", "response_invalid"]
)
async def test_handler_composition_records_only_committed_stops(
    session_factory,
    claimed,
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
            lease_token=claimed.lease_token,
            leased_until=FUTURE,
            attempt_count=1,
        )
        await seed.commit()
    claimed = replace(claimed, event_id=event_id)
    if mode == "skipped":
        claimed = replace(claimed, lease_token=uuid4())

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
            error = PublishIntegrityError(
                reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH
            )
        elif failure_kind == "response_invalid":
            error = PublishResponseInvalidError(
                reason=PublishResponseInvalidReason.MISSING_ENTRY_ID,
            )
        else:
            error = CASES[0][0]
        result = await OutboxDeliveryFailureHandler(factory, jitter=lambda: 0.5).handle(
            event=claimed, failure=PublishFailed(claimed.event_id, error)
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
        assert stop["error_reason"] == "body_checksum_mismatch"
        assert "自動再試行を停止しました" in stop["error_message"]
    if failure_kind == "response_invalid" and mode == "committed":
        stop = next(log for log in logs if log["event"] == "outbox_delivery_stopped")
        assert stop["error_reason"] == "missing_entry_id"
        assert "response_field" not in stop
        assert not stop["requires_configuration_fix"]


def test_integrity_failure_message_is_fixed_and_private_diagnostics_are_excluded(
    claimed, capsys
):
    """本文不一致の説明は固定文とし、機密情報を記録しない。"""
    error = PublishIntegrityError(
        reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH, request_id=PRIVATE
    )
    error.__cause__ = RuntimeError(PRIVATE)
    error.MESSAGE = PRIVATE
    with capture_logs() as logs:
        recording.record_publish_failure(
            event=claimed, error=error, stop_reason=STOP_REASON
        )
    assert capsys.readouterr().out == ""
    assert not recording.requires_publish_configuration_fix(error)
    assert len(logs) == 1
    assert logs[0]["error_message"] == (
        "送信した本文と、送信先が受け取った本文のチェックサムが一致しません。"
        "送信先には受付済みの可能性があるため、このイベントの自動再試行を停止しました。"
    )
    assert logs[0]["error_reason"] == "body_checksum_mismatch"
    assert logs[0]["error_code"] == "publish_integrity_error"
    assert PRIVATE not in json.dumps(logs)
    assert "request_id" not in logs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sink", ["log", "metric", "both"])
async def test_handler_returns_committed_stop_despite_output_failure(
    session_factory, claimed, failed_sink, monkeypatch
):
    """記録先の障害でも停止の確定結果を維持し、再処理で記録を繰り返さない。"""
    from dataclasses import replace

    async with session_factory() as seed:
        event_id = await insert_event(
            seed,
            next_attempt_at=PAST,
            lease_token=claimed.lease_token,
            leased_until=FUTURE,
            attempt_count=1,
        )
        await seed.commit()
    claimed = replace(claimed, event_id=event_id)
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
    failure = PublishFailed(event_id, CASES[0][0])
    assert await handler.handle(event=claimed, failure=failure) == DeliveryStopped(
        STOP_REASON
    )
    async with session_factory() as reader:
        saved = await read_event(reader, event_id)
    assert saved["delivery_stop_reason"] == STOP_REASON.value
    assert saved["delivery_stopped_at"] is not None
    assert saved["lease_token"] is None
    assert (
        await handler.handle(event=claimed, failure=failure) == DeliveryUpdateSkipped()
    )
    logger.info.assert_called_once()
    metric.assert_called_once()
    async with session_factory() as reader:
        assert await read_event(reader, event_id) == saved
