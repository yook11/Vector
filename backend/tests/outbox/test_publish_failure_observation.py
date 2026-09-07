"""設定修復の通知対象と観測出力の境界を検証する。"""

import json
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox import publish_failure_observation as observation
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.publish_failure_handler import (
    DeliveryStopped,
    DeliveryUpdateSkipped,
    PublishFailureHandler,
    RetryScheduled,
)
from app.outbox.publish_failure_policy import NonRetryableReason
from app.outbox.repository import ClaimedOutboxEvent
from tests.cloudwatch.records import emf_records
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

PRIVATE = "PRIVATE_PAYLOAD_URL_CREDENTIAL"
STOPPED = DeliveryStopped(NonRetryableReason.NON_RETRYABLE_FAILURE)
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
    (PublishError(), False),
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
    """通知対象だけ打点し、ログやEMFへ自由文と元例外を出さない。"""
    error.__cause__ = RuntimeError(PRIVATE)
    assert observation.requires_publish_configuration_fix(error) is expected
    with capture_logs() as logs:
        observation.record_publish_failure(event=claimed, error=error, result=STOPPED)
    records = emf_records(capsys.readouterr().out)
    assert len(records) == int(expected)
    assert len(logs) == 1
    log = logs[0]
    assert log["event"] == "outbox_delivery_stopped"
    assert log["event_id"] == str(claimed.event_id)
    assert log["attempt_count"] == claimed.attempt_count
    assert log["stop_reason"] == STOPPED.reason.value
    assert log["error_code"] == error.CODE
    assert log["requires_configuration_fix"] is expected
    if isinstance(error, PublishTransportError):
        assert log["transport_kind"] == error.failure.kind.value
    elif isinstance(error, PublishUnexpectedError):
        assert log["phase"] == error.phase.value
        assert log["original_exception_type"] == "builtins.RuntimeError"
        assert log["classification_exception_type"] == "builtins.ValueError"
    elif isinstance(
        error,
        (PublishServiceError, PublishConfigurationError, PublishEventInvalidError),
    ):
        assert log["error_reason"] == error.reason.value
    assert PRIVATE not in json.dumps([logs, records])
    if expected:
        record = records[0]
        assert record[observation.CONFIGURATION_FAILURE_METRIC] == 1
        directive = record["_aws"]["CloudWatchMetrics"][0]
        assert directive == {
            "Namespace": "Vector/Pipeline",
            "Dimensions": [[]],
            "Metrics": [
                {"Name": observation.CONFIGURATION_FAILURE_METRIC, "Unit": "Count"}
            ],
        }
        assert set(record) == {"_aws", observation.CONFIGURATION_FAILURE_METRIC}


@pytest.mark.parametrize(
    "result", [RetryScheduled(timedelta(seconds=30)), DeliveryUpdateSkipped()]
)
def test_non_stopped_results_emit_nothing(result, claimed, capsys):
    """停止未確定の結果ではログもメトリクスも出さない。"""
    with capture_logs() as logs:
        observation.record_publish_failure(
            event=claimed, error=CASES[0][0], result=result
        )
    assert logs == []
    assert capsys.readouterr().out == ""


def test_retry_exhaustion_alone_is_not_alarm_target(claimed, capsys):
    """再試行上限は人の設定修復が必要という根拠にしない。"""
    with capture_logs() as logs:
        observation.record_publish_failure(
            event=claimed,
            error=PublishTransportError(
                failure=HttpTransportFailure(
                    HttpTransportFailureKind.READ_TIMEOUT, True
                )
            ),
            result=DeliveryStopped(NonRetryableReason.RETRY_EXHAUSTED),
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
    monkeypatch.setattr(observation, "logger", logger)
    monkeypatch.setattr(observation, "emit_metric", metric)
    observation.record_publish_failure(event=claimed, error=CASES[0][0], result=STOPPED)
    logger.info.assert_called_once()
    metric.assert_called_once()
    assert logger.warning.call_count == (2 if failed_sink == "both" else 1)
    assert PRIVATE not in str(logger.warning.call_args_list)


@pytest.mark.parametrize("sink", ["log", "metric"])
def test_base_exception_is_not_swallowed(sink, claimed, monkeypatch):
    """プロセス終了を観測の通常障害として握りつぶさない。"""
    failure = KeyboardInterrupt()
    if sink == "log":
        logger = Mock()
        logger.info.side_effect = failure
        monkeypatch.setattr(observation, "logger", logger)
    else:
        monkeypatch.setattr(observation, "emit_metric", Mock(side_effect=failure))
    with pytest.raises(KeyboardInterrupt) as caught:
        observation.record_publish_failure(
            event=claimed, error=CASES[0][0], result=STOPPED
        )
    assert caught.value is failure


def test_alarm_matches_emitted_metric_and_only_notifies_alarm_state():
    """Terraformとアプリの系列名・評価条件・通知経路の一致を固定する。"""
    source = (Path(__file__).resolve().parents[3] / "infra/aws/alerting.tf").read_text()
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
        "metric_name": json.dumps(observation.CONFIGURATION_FAILURE_METRIC),
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
async def test_handler_composition_records_only_committed_stops(
    session_factory,
    claimed,
    mode,
    capsys,
):
    """停止の可視性と、commit失敗・更新なしで観測しない境界を実DBで確認する。"""
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

    @asynccontextmanager
    async def factory():
        async with session_factory() as session:
            if mode == "commit_failed":
                sqlalchemy_event.listen(
                    session.sync_session, "before_commit", fail_commit
                )
            yield session

    async def handle_and_record():
        error = CASES[0][0]
        result = await PublishFailureHandler(factory, jitter=lambda: 0.5).handle(
            event=claimed, error=error
        )
        async with session_factory() as reader:
            saved = await read_event(reader, event_id)
        assert (saved["delivery_stopped_at"] is not None) == (mode == "committed")
        observation.record_publish_failure(event=claimed, error=error, result=result)

    with capture_logs() as logs:
        if mode == "commit_failed":
            with pytest.raises(SQLAlchemyError):
                await handle_and_record()
        else:
            await handle_and_record()
    assert len(
        [log for log in logs if log["event"] == "outbox_delivery_stopped"]
    ) == int(mode == "committed")
    assert len(emf_records(capsys.readouterr().out)) == int(mode == "committed")
