"""バッチの受付結果と、個別失敗・全体失敗の境界を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import botocore.session
import pytest
from botocore.credentials import ReadOnlyCredentials
from botocore.exceptions import ClientError, HTTPClientError, ReadTimeoutError
from botocore.stub import Stubber

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.outbox.delivery.repository import ClaimedOutboxEvent
from app.outbox.publishing.errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
    PublishResponseInvalidError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.sqs import publisher as publisher_module
from app.outbox.sqs.error_mapping import SqsBatchEntryError
from app.outbox.sqs.message import SqsMessage
from app.outbox.sqs.publisher import SqsEventPublisher
from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"
LIMIT = 512


@pytest.fixture
def envelope():
    return EventEnvelope(
        event_id=UUID(int=1),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


@pytest.fixture
def cleanup_log(monkeypatch):
    log = Mock()
    monkeypatch.setattr(publisher_module.logger, "warning", log)
    return log


def _success(envelope):
    return {
        "Id": str(envelope.event_id),
        "MessageId": "sqs-id",
        "MD5OfMessageBody": md5(
            _body(envelope).encode("utf-8"), usedforsecurity=False
        ).hexdigest(),
    }


def _failure(event_id, code="AccessDenied", sender_fault=True):
    return {
        "Id": str(event_id),
        "Code": code,
        "SenderFault": sender_fault,
        "Message": "PRIVATE",
    }


def _event(envelope):
    return ArticleAssessedInScopeEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        schema_version=envelope.schema_version,
        occurred_at=envelope.occurred_at,
        payload=envelope.payload,
    )


def _body(envelope):
    return json.dumps(
        {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "schema_version": envelope.schema_version,
            "occurred_at": envelope.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": envelope.payload,
        },
        allow_nan=False,
    )


def _sender(*, response=None, error=None, close_error=None):
    client = Mock()
    client.send_message_batch.return_value = response
    client.send_message_batch.side_effect = error
    client.close.side_effect = close_error
    factory = Mock(return_value=client)
    return (
        SqsEventPublisher(client_factory=factory, embedding_queue_url=QUEUE_URL),
        client,
        factory,
    )


@pytest.mark.parametrize("count", [1, 10])
def test_batch_success_preserves_body_and_input_order_on_resend(envelope, count):
    """SQSの応答順によらず入力順を返し、再送でも保存済み本文を維持する。"""
    events = [replace(envelope, event_id=UUID(int=i + 1)) for i in range(count)]
    original = deepcopy(events)
    client = botocore.session.get_session().create_client(
        "sqs",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    sender = SqsEventPublisher(
        client_factory=lambda: client, embedding_queue_url=QUEUE_URL
    )
    params = {
        "QueueUrl": QUEUE_URL,
        "Entries": [{"Id": str(e.event_id), "MessageBody": _body(e)} for e in events],
    }
    try:
        with Stubber(client) as stubber:
            for _ in range(2):
                stubber.add_response(
                    "send_message_batch",
                    {
                        "Successful": [_success(e) for e in reversed(events)],
                        "Failed": [],
                    },
                    params,
                )
                assert sender.publish_batch(events) == BatchPublishResult(
                    tuple(PublishSucceeded(e.event_id) for e in events)
                )
            stubber.assert_no_pending_responses()
    finally:
        client.close()
    assert events == original


def test_partial_failure_keeps_success_and_batch_request_id(envelope):
    """HTTP 200の部分失敗に個別statusを捏造せず、request IDだけを引き継ぐ。"""
    second = replace(envelope, event_id=UUID(int=2))
    sender, client, _ = _sender(
        response={
            "Successful": [_success(second)],
            "Failed": [_failure(envelope.event_id)],
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-id"},
        }
    )
    result = sender.publish_batch([envelope, second])
    failed, succeeded = result.results
    assert isinstance(failed, PublishFailed)
    assert isinstance(failed.error, PublishServiceError)
    assert failed.error.reason is PublishServiceReason.ACCESS_DENIED
    assert failed.error.status_code is None
    assert failed.error.request_id == "request-id"
    assert succeeded == PublishSucceeded(second.event_id)
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("sender_fault", [False, True])
def test_unknown_entry_code_is_unclassified_regardless_of_sender_fault(
    envelope, sender_fault
):
    sender, _, _ = _sender(
        response={"Failed": [_failure(envelope.event_id, "FutureCode", sender_fault)]}
    )
    error = sender.publish_batch([envelope]).results[0].error
    assert error.reason is PublishServiceReason.UNCLASSIFIED
    assert error.service_error_code == "FutureCode"
    assert error.status_code is None
    assert error.request_id is None


@pytest.mark.parametrize(
    "field,value,reason",
    [
        (
            "event_type",
            "article.acquired",
            PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE,
        ),
        (
            "occurred_at",
            datetime(2026, 9, 7),
            PublishEventInvalidReason.INVALID_ENVELOPE,
        ),
        ("payload", {"bad": object()}, PublishEventInvalidReason.INVALID_PAYLOAD),
        (
            "payload",
            {"bad": float("nan")},
            PublishEventInvalidReason.INVALID_PAYLOAD,
        ),
        ("payload", {"bad": "x" * LIMIT}, PublishEventInvalidReason.INVALID_PAYLOAD),
        ("schema_version", 2, PublishEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION),
        (
            "payload",
            {"curation_id": True, "analyzed_article_id": 1},
            PublishEventInvalidReason.INVALID_PAYLOAD,
        ),
    ],
)
@pytest.mark.parametrize("include_valid", [False, True])
def test_invalid_event_does_not_prevent_other_events(
    envelope, field, value, reason, include_valid
):
    """不正イベントだけを除外し、全件不正なら資格情報も取得しない。"""
    bad = replace(envelope, **{field: value})
    good = replace(envelope, event_id=UUID(int=2))
    sender, client, factory = _sender(response={"Successful": [_success(good)]})
    result = sender.publish_batch([bad, good] if include_valid else [bad])
    assert isinstance(result.results[0].error, PublishEventInvalidError)
    assert result.results[0].error.reason is reason
    if include_valid:
        assert result.results[1] == PublishSucceeded(good.event_id)
        assert client.send_message_batch.call_args.kwargs["Entries"] == [
            {"Id": str(good.event_id), "MessageBody": _body(good)}
        ]
    else:
        factory.assert_not_called()


@pytest.mark.parametrize("event_type", ["article.acquired", "future.event"])
@pytest.mark.parametrize("include_valid", [False, True])
def test_unsupported_event_is_rejected_before_body_preparation(
    envelope, event_type, include_valid, monkeypatch
):
    """対応外の種別を本文不正より先に拒否し、正常分だけを送信する。"""
    bad = replace(
        envelope,
        event_type=event_type,
        occurred_at=datetime(2026, 9, 7),
        payload={"bad": object()},
    )
    good = replace(envelope, event_id=UUID(int=2))
    prepare = Mock(wraps=SqsMessage.from_event)
    monkeypatch.setattr(SqsMessage, "from_event", prepare)
    sender, client, factory = _sender(response={"Successful": [_success(good)]})

    result = sender.publish_batch([bad, good] if include_valid else [bad])

    assert isinstance(result.results[0], PublishFailed)
    assert result.results[0].event_id == bad.event_id
    assert (
        result.results[0].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    if include_valid:
        prepare.assert_called_once_with(_event(good))
        assert result.results[1] == PublishSucceeded(good.event_id)
        client.send_message_batch.assert_called_once_with(
            QueueUrl=QUEUE_URL,
            Entries=[{"Id": str(good.event_id), "MessageBody": _body(good)}],
        )
    else:
        prepare.assert_not_called()
        factory.assert_not_called()
        client.send_message_batch.assert_not_called()


@pytest.mark.parametrize(
    "case,exception",
    [
        ("empty", ValueError),
        ("eleven", ValueError),
        ("duplicate", ValueError),
        ("none", TypeError),
        ("string", TypeError),
        ("iterator", TypeError),
        ("element", TypeError),
        ("event_id", TypeError),
        ("event_type", TypeError),
        ("schema_version", TypeError),
        ("occurred_at", TypeError),
        ("payload", TypeError),
    ],
)
def test_call_contract_violations_precede_client_creation(envelope, case, exception):
    inputs = {
        "empty": [],
        "eleven": [replace(envelope, event_id=UUID(int=i)) for i in range(11)],
        "duplicate": [envelope, envelope],
        "none": None,
        "string": "private",
        "iterator": iter([envelope]),
        "element": [object()],
        "event_id": [replace(envelope, event_id="private")],
        "event_type": [replace(envelope, event_type=1)],
        "schema_version": [replace(envelope, schema_version=True)],
        "occurred_at": [replace(envelope, occurred_at="private")],
        "payload": [replace(envelope, payload=[])],
    }
    sender, _, factory = _sender()
    with pytest.raises(exception):
        sender.publish_batch(inputs[case])
    factory.assert_not_called()


def test_input_count_is_checked_before_excluding_invalid_events(envelope, monkeypatch):
    """準備失敗を除けば10件になる入力でも、11件の呼び出し自体を拒否する。"""
    events = [replace(envelope, event_id=UUID(int=index)) for index in range(11)]
    events[0] = replace(events[0], event_type="unsupported")
    prepare = Mock(side_effect=AssertionError("preparation must not run"))
    monkeypatch.setattr(SqsMessage, "from_event", prepare)
    sender, _, factory = _sender()
    with pytest.raises(ValueError):
        sender.publish_batch(events)
    prepare.assert_not_called()
    factory.assert_not_called()


def test_all_invalid_events_do_not_construct_an_empty_batch(envelope, monkeypatch):
    """送信対象がなければ、空バッチの構築ではなく個別失敗の返却で完了する。"""
    make_batch = Mock(side_effect=AssertionError("batch must not be created"))
    monkeypatch.setattr("app.outbox.sqs.publisher.SqsMessageBatch", make_batch)
    sender, _, factory = _sender()
    result = sender.publish_batch([replace(envelope, event_type="unsupported")])
    assert (
        result.results[0].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    make_batch.assert_not_called()
    factory.assert_not_called()


@pytest.fixture
def small_message_limit(monkeypatch):
    """正しいpayloadで個別・合計サイズの境界へ到達できる上限を使う。"""
    monkeypatch.setattr("app.outbox.sqs.message.MAX_MESSAGE_BYTES", LIMIT)
    monkeypatch.setattr("app.outbox.sqs.message_batch.MAX_MESSAGE_BYTES", LIMIT)


def _sized(envelope, size):
    base = replace(envelope, payload={"curation_id": 1, "analyzed_article_id": 456})
    digits = size - len(_body(base).encode("utf-8")) + 1
    return replace(
        base, payload={"curation_id": int("9" * digits), "analyzed_article_id": 456}
    )


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("extra", [0, 1])
def test_message_and_batch_byte_limits(envelope, count, extra, small_message_limit):
    """個別超過はイベント失敗、正常な本文の合計超過は呼び出し違反とする。"""
    events = [
        _sized(
            replace(envelope, event_id=UUID(int=i + 1)),
            LIMIT // count + (extra if i == 0 else 0),
        )
        for i in range(count)
    ]
    assert sum(len(_body(e).encode("utf-8")) for e in events) == LIMIT + extra
    sender, client, factory = _sender(
        response={"Successful": [_success(e) for e in events]}
    )
    if extra and count == 2:
        with pytest.raises(ValueError):
            sender.publish_batch(events)
        factory.assert_not_called()
    elif extra:
        assert (
            sender.publish_batch(events).results[0].error.reason
            is PublishEventInvalidReason.MESSAGE_TOO_LARGE
        )
        factory.assert_not_called()
    else:
        assert all(
            isinstance(r, PublishSucceeded)
            for r in sender.publish_batch(events).results
        )
        client.send_message_batch.assert_called_once()


def test_non_ascii_payload_is_rejected_before_serialization(envelope):
    """サイズの大きい文字列でも、payload契約違反を先に拒否する。"""
    event = replace(envelope, payload={"text": "あ" * (LIMIT // 6)})
    assert len(_body(event).encode("utf-8")) > LIMIT
    sender, _, factory = _sender()
    assert (
        sender.publish_batch([event]).results[0].error.reason
        is PublishEventInvalidReason.INVALID_PAYLOAD
    )
    factory.assert_not_called()


def test_utc_precision_and_claimed_payload_copy(envelope):
    """時刻の意味と小数秒を保ち、確保情報や本文の参照を送信へ持ち込まない。"""
    claimed = ClaimedOutboxEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        schema_version=1,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
        payload=deepcopy(envelope.payload),
        attempt_count=2,
        lease_token=UUID(int=2),
        leased_until=datetime(2026, 9, 7, 4, tzinfo=UTC),
    )
    event = EventEnvelope.from_claimed(claimed)
    claimed.payload["curation_id"] = 999
    sender, client, _ = _sender(response={"Successful": [_success(event)]})
    sender.publish_batch([event])
    body = json.loads(
        client.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    )
    assert body == {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "schema_version": 1,
        "occurred_at": "2026-09-07T03:00:00.123456Z",
        "payload": {"curation_id": 123, "analyzed_article_id": 456},
    }


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "private"}},
                "SendMessageBatch",
            ),
            PublishServiceError,
        ),
        (ReadTimeoutError(endpoint_url="private"), PublishTransportError),
        (HTTPClientError(error="private"), PublishTransportError),
        (RuntimeError("private"), PublishUnexpectedError),
    ],
)
@pytest.mark.parametrize("close_fails", [False, True])
def test_whole_request_failure_preserves_local_failure_and_cleanup(
    envelope, exc, expected, close_fails, cleanup_log
):
    """全体失敗を送信対象へ反映し、ローカルの不正や先行障害を上書きしない。"""
    cleanup_log.side_effect = RuntimeError("private log failure")
    good = replace(envelope, event_id=UUID(int=2))
    bad = replace(envelope, event_id=UUID(int=3), event_type="unsupported")
    sender, client, _ = _sender(
        error=exc, close_error=ValueError("private") if close_fails else None
    )
    result = sender.publish_batch([envelope, good, bad])
    for outcome in result.results[:2]:
        assert isinstance(outcome.error, expected)
        assert outcome.error.__cause__ is exc
    assert (
        result.results[2].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    assert cleanup_log.call_count == int(close_fails)
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize(
    "close_error",
    [
        RuntimeError("private"),
        PublishConfigurationError(
            reason=PublishConfigurationReason.MISSING_CREDENTIALS
        ),
    ],
)
@pytest.mark.parametrize("logging_failure", [False, True])
def test_cleanup_failure_preserves_success(
    envelope, close_error, logging_failure, cleanup_log
):
    """終了失敗を安全に記録し、診断出力が失敗しても受付結果を返す。"""
    if logging_failure:
        cleanup_log.side_effect = RuntimeError("private log failure")
    sender, _, _ = _sender(
        response={"Successful": [_success(envelope)]}, close_error=close_error
    )
    result = sender.publish_batch([envelope])
    assert result.results == (PublishSucceeded(envelope.event_id),)
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed",
        error_code="publish_cleanup_error",
        original_exception_type=f"{type(close_error).__module__}.{type(close_error).__qualname__}",
    )


def test_whole_request_classifier_failure_keeps_both_types(envelope, monkeypatch):
    original = RuntimeError("original private")
    monkeypatch.setattr(
        "app.outbox.sqs.error_mapping._publish_error_from_sqs_exception",
        Mock(side_effect=ValueError("classifier private")),
    )
    sender, _, _ = _sender(error=original)
    error = sender.publish_batch([envelope]).results[0].error
    assert error.phase is PublishPhase.CLASSIFY_FAILURE
    assert error.original_exception_type == "builtins.RuntimeError"
    assert error.classification_exception_type == "builtins.ValueError"
    assert error.__cause__ is original


def test_entry_classifier_failure_preserves_other_results(envelope, monkeypatch):
    """応答対応付けの完了後は、個別の分類障害を該当イベントに限定する。"""
    from app.outbox.sqs.error_mapping import _service_error_from_sqs_code

    def classify(**kwargs):
        if kwargs["code"] == "Bad":
            raise ValueError("private")
        return _service_error_from_sqs_code(**kwargs)

    monkeypatch.setattr(
        "app.outbox.sqs.error_mapping._service_error_from_sqs_code", classify
    )
    events = [replace(envelope, event_id=UUID(int=i)) for i in [1, 2, 3]]
    sender, _, _ = _sender(
        response={
            "Successful": [_success(events[0])],
            "Failed": [
                _failure(events[1].event_id, "Bad"),
                _failure(events[2].event_id, "RequestThrottled"),
            ],
        }
    )
    result = sender.publish_batch(events)
    assert result.results[0] == PublishSucceeded(events[0].event_id)
    error = result.results[1].error
    assert error.phase is PublishPhase.CLASSIFY_FAILURE
    assert isinstance(error.__cause__, SqsBatchEntryError)
    assert error.__cause__.code == "Bad"
    assert error.original_exception_type == (
        "app.outbox.sqs.error_mapping.SqsBatchEntryError"
    )
    assert error.classification_exception_type == "builtins.ValueError"
    assert result.results[2].error.reason is PublishServiceReason.THROTTLED


@pytest.mark.parametrize(
    "case",
    [
        "root",
        "list",
        "entry",
        "missing",
        "unknown",
        "duplicate",
        "overlap",
        "message_id",
        "md5",
        "code",
        "sender_fault",
    ],
)
def test_malformed_response_invalidates_all_sent_results(envelope, case):
    """不正応答ではHTTP成功や一部の見かけ上の成功を受付確定に使わない。"""
    second = replace(envelope, event_id=UUID(int=2))
    response = {
        "Successful": [_success(envelope)],
        "Failed": [_failure(second.event_id)],
    }
    if case == "root":
        response = None
    elif case == "list":
        response["Failed"] = {}
    elif case == "entry":
        response["Failed"] = ["private"]
    elif case == "missing":
        response["Failed"] = []
    elif case == "unknown":
        response["Failed"][0]["Id"] = str(UUID(int=3))
    elif case == "duplicate":
        response["Failed"] *= 2
    elif case == "overlap":
        response["Failed"].append(_failure(envelope.event_id))
    elif case == "message_id":
        response["Successful"][0]["MessageId"] = ""
    elif case == "md5":
        response["Successful"][0].pop("MD5OfMessageBody")
    elif case == "code":
        response["Failed"][0]["Code"] = 123
    elif case == "sender_fault":
        response["Failed"][0]["SenderFault"] = "true"
    sender, client, _ = _sender(response=response)
    result = sender.publish_batch([envelope, second])
    assert len(result.results) == 2
    for outcome in result.results:
        assert isinstance(outcome.error, PublishResponseInvalidError)
        expected = {
            "root": ("invalid_type", "response"),
            "list": ("invalid_type", "failed_entries"),
            "entry": ("invalid_type", "failed_entry"),
            "missing": ("missing_entry_id", "entry_id"),
            "unknown": ("unknown_entry_id", "entry_id"),
            "duplicate": ("duplicate_entry_id", "entry_id"),
            "overlap": ("duplicate_entry_id", "entry_id"),
            "message_id": ("empty_required_field", "message_id"),
            "md5": ("missing_required_field", "body_checksum"),
            "code": ("invalid_type", "error_code"),
            "sender_fault": ("invalid_type", "sender_fault"),
        }
        assert (
            outcome.error.reason.value,
            outcome.error.__cause__.field.value,
        ) == expected[case]
        assert isinstance(outcome.error.__cause__, InvalidSqsBatchResponse)
        assert outcome.error.__cause__.reason is outcome.error.reason
        assert not hasattr(outcome.error, "field")
        assert outcome.error.__cause__.args == ()
    client.close.assert_called_once()


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_process_exit_is_not_wrapped_or_replaced_by_cleanup(envelope, exc, cleanup_log):
    cleanup_log.side_effect = RuntimeError("private log failure")
    sender, client, _ = _sender(error=exc, close_error=ValueError("cleanup"))
    with pytest.raises(type(exc)) as caught:
        sender.publish_batch([envelope])
    assert caught.value is exc
    client.close.assert_called_once()


@pytest.mark.parametrize("phase", ["initialize", "send"])
def test_existing_publish_error_is_not_wrapped(envelope, phase):
    original = PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )
    sender, client, factory = _sender(error=original if phase == "send" else None)
    if phase == "initialize":
        factory.side_effect = original
    error = sender.publish_batch([envelope]).results[0].error
    assert error is original
    assert error.__cause__ is None
    if phase == "initialize":
        client.send_message_batch.assert_not_called()


def test_each_batch_freezes_credentials_and_closes_its_own_client(envelope):
    session = Mock()
    clients = [Mock(), Mock()]
    for client in clients:
        client.send_message_batch.return_value = {"Successful": [_success(envelope)]}
    session.create_client.side_effect = clients
    freeze = session.get_credentials.return_value.get_frozen_credentials
    freeze.side_effect = [
        ReadOnlyCredentials("first", "secret", "token1"),
        ReadOnlyCredentials("second", "secret", "token2"),
    ]
    sender = SqsEventPublisher.from_session(
        session=session, region="ap-northeast-1", embedding_queue_url=QUEUE_URL
    )
    for _ in range(2):
        assert sender.publish_batch([envelope]).results == (
            PublishSucceeded(envelope.event_id),
        )
    assert freeze.call_count == 2
    assert [
        call.kwargs["aws_access_key_id"]
        for call in session.create_client.call_args_list
    ] == ["first", "second"]
    assert (
        clients[0].send_message_batch.call_args
        == clients[1].send_message_batch.call_args
    )
    for client in clients:
        client.send_message_batch.assert_called_once()
        client.close.assert_called_once()


@pytest.mark.parametrize(
    "exc",
    [
        ReadTimeoutError(endpoint_url="private"),
        ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"),
    ],
)
def test_credential_provider_failure_is_not_sqs_failure(envelope, exc):
    session = Mock()
    session.get_credentials.side_effect = exc
    sender = SqsEventPublisher.from_session(
        session=session, region="ap-northeast-1", embedding_queue_url=QUEUE_URL
    )
    error = sender.publish_batch([envelope]).results[0].error
    assert isinstance(error, PublishConfigurationError)
    assert error.reason is PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED
    assert error.__cause__ is exc
    session.create_client.assert_not_called()


def test_unexpected_initialization_failure_retains_phase(envelope):
    sender, _, factory = _sender()
    factory.side_effect = RuntimeError("private")
    error = sender.publish_batch([envelope]).results[0].error
    assert error.phase is PublishPhase.INITIALIZE
    assert error.__cause__ is factory.side_effect


def test_preparation_unexpected_failure_is_local(envelope, monkeypatch):
    sender, _, factory = _sender()
    exc = RuntimeError("private")
    monkeypatch.setattr(SqsMessage, "from_event", Mock(side_effect=exc))
    error = sender.publish_batch([envelope]).results[0].error
    assert error.phase is PublishPhase.PREPARE_EVENT
    assert error.__cause__ is exc
    factory.assert_not_called()


def test_results_are_frozen_and_do_not_expose_diagnostics(envelope, cleanup_log):
    marker = "PRIVATE_BODY_CREDENTIAL_QUEUE"
    sender, _, _ = _sender(
        response={
            "Failed": [_failure(envelope.event_id, marker)],
            "ResponseMetadata": {"RequestId": marker},
        },
        close_error=RuntimeError(marker),
    )
    result = sender.publish_batch([envelope])
    assert marker not in repr(result)
    assert marker not in str(result.results[0].error)
    assert marker not in str(cleanup_log.call_args)
    for obj, attr, value in [
        (result, "results", ()),
        (result.results[0], "event_id", UUID(int=2)),
        (PublishSucceeded(envelope.event_id), "event_id", UUID(int=2)),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, value)


def test_oversized_event_stops_without_configuration_alarm(
    envelope, small_message_limit
):
    from app.outbox.delivery.failure_recording import (
        requires_publish_configuration_fix,
    )
    from app.outbox.delivery.retry_policy import (
        NonRetryable,
        NonRetryableReason,
        decide_publish_retry,
    )

    sender, _, _ = _sender()
    error = sender.publish_batch([_sized(envelope, LIMIT + 1)]).results[0].error
    assert decide_publish_retry(error, attempt_count=1, jitter=0.5) == NonRetryable(
        NonRetryableReason.NON_RETRYABLE_FAILURE
    )
    assert not requires_publish_configuration_fix(error)


@pytest.mark.parametrize("metadata", [None, "private", {}, {"RequestId": 123}])
def test_all_entries_can_fail_with_missing_metadata(envelope, metadata):
    """全件の個別失敗を入力へ対応付け、任意metadata欠損で原因を失わない。"""
    second = replace(envelope, event_id=UUID(int=2))
    sender, _, _ = _sender(
        response={
            "Failed": [
                _failure(second.event_id, "RequestThrottled"),
                _failure(envelope.event_id),
            ],
            "ResponseMetadata": metadata,
        }
    )
    result = sender.publish_batch([envelope, second])
    assert [r.event_id for r in result.results] == [envelope.event_id, second.event_id]
    assert [r.error.reason for r in result.results] == [
        PublishServiceReason.ACCESS_DENIED,
        PublishServiceReason.THROTTLED,
    ]
    assert all(
        r.error.request_id is None and r.error.status_code is None
        for r in result.results
    )


def test_initialization_failure_preserves_invalid_event(envelope):
    """送信対象を作れなくても、既知のイベント不正は設定失敗に置き換えない。"""
    bad = replace(envelope, event_id=UUID(int=2), event_type="unsupported")
    original = PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )
    sender, _, factory = _sender()
    factory.side_effect = original
    result = sender.publish_batch([bad, envelope])
    assert (
        result.results[0].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    assert result.results[1].error is original


@pytest.mark.parametrize(
    "body,expected_md5",
    [
        ("{}", "99914b932bd37a50b983c5e7c90ae93b"),
        (' { "text": "日本語\\n\\"引用\\"" }\n', None),
    ],
)
def test_checksum_uses_exact_transmitted_body(
    envelope, monkeypatch, body, expected_md5
):
    """固定の既知MD5と非ASCII本文で、再JSON化せず送信文字列を照合する。"""
    expected_md5 = (
        expected_md5 or md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()
    )
    sender, client, _ = _sender(
        response={
            "Successful": [
                {
                    "Id": str(envelope.event_id),
                    "MessageId": "sqs-id",
                    "MD5OfMessageBody": expected_md5,
                }
            ],
        }
    )
    prepared = SqsMessage(event_id=envelope.event_id, body=body)
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(SqsMessage, "from_event", prepare)
    assert sender.publish_batch([envelope]).results == (
        PublishSucceeded(envelope.event_id),
    )
    prepare.assert_called_once_with(_event(envelope))
    assert client.send_message_batch.call_args.kwargs["Entries"] == [
        {"Id": str(envelope.event_id), "MessageBody": body}
    ]


@pytest.mark.parametrize("include_valid", [False, True])
def test_checksum_calculation_failure_is_local_before_send(
    envelope, monkeypatch, include_valid
):
    """MD5計算障害を準備段階の失敗とし、残りの正常イベントだけ送る。"""
    second = replace(envelope, event_id=UUID(int=2))
    exc = ValueError("PRIVATE")
    hash_call = Mock(
        side_effect=[exc, md5(_body(second).encode("utf-8"), usedforsecurity=False)]
    )
    monkeypatch.setattr("app.outbox.sqs.message.md5", hash_call)
    sender, client, factory = _sender(response={"Successful": [_success(second)]})
    result = sender.publish_batch([envelope, second] if include_valid else [envelope])
    error = result.results[0].error
    assert isinstance(error, PublishUnexpectedError)
    assert error.phase is PublishPhase.PREPARE_EVENT
    assert error.__cause__ is exc
    assert all(
        call.kwargs == {"usedforsecurity": False} for call in hash_call.call_args_list
    )
    if include_valid:
        assert result.results[1] == PublishSucceeded(second.event_id)
        assert len(client.send_message_batch.call_args.kwargs["Entries"]) == 1
    else:
        factory.assert_not_called()


def test_mixed_integrity_and_service_failures_keep_success_and_cleanup(
    envelope, cleanup_log
):
    """本文不一致・サービス拒否・終了障害が混在しても成功を上書きしない。"""
    from app.outbox.publishing.errors import (
        PublishIntegrityError,
        PublishIntegrityReason,
    )

    events = [replace(envelope, event_id=UUID(int=i)) for i in [1, 2, 3]]
    mismatch = _success(events[1])
    mismatch["MD5OfMessageBody"] = "0" * 32
    close_error = RuntimeError("PRIVATE")
    sender, client, _ = _sender(
        response={
            "Successful": [mismatch, _success(events[0])],
            "Failed": [_failure(events[2].event_id, "RequestThrottled")],
            "ResponseMetadata": {"RequestId": "request-id"},
        },
        close_error=close_error,
    )
    result = sender.publish_batch(events)
    assert [r.event_id for r in result.results] == [e.event_id for e in events]
    assert result.results[0] == PublishSucceeded(events[0].event_id)
    assert isinstance(result.results[1].error, PublishIntegrityError)
    assert (
        result.results[1].error.reason is PublishIntegrityReason.BODY_CHECKSUM_MISMATCH
    )
    assert result.results[1].error.request_id == "request-id"
    assert result.results[2].error.reason is PublishServiceReason.THROTTLED
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed",
        error_code="publish_cleanup_error",
        original_exception_type=f"{type(close_error).__module__}.{type(close_error).__qualname__}",
    )
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("case", ["bad_checksum", "unknown_id"])
def test_response_invalid_preserves_local_failure_and_cleanup_without_raw_values(
    envelope, case, cleanup_log
):
    """応答違反を全送信対象へ反映し、送信前の不正と終了失敗を上書きしない。"""
    from app.outbox.publishing.errors import PublishResponseInvalidReason

    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    invalid = replace(envelope, event_id=UUID(int=2), event_type="unsupported")
    entry = _success(envelope)
    entry["MD5OfMessageBody" if case == "bad_checksum" else "Id"] = marker
    response = {"Successful": [entry], "ResponseMetadata": {"RequestId": marker}}
    sender, client, _ = _sender(response=response, close_error=RuntimeError(marker))
    result = sender.publish_batch([invalid, envelope])
    assert isinstance(result.results[0].error, PublishEventInvalidError)
    error = result.results[1].error
    assert isinstance(error, PublishResponseInvalidError)
    assert error.reason is (
        PublishResponseInvalidReason.INVALID_CHECKSUM_FORMAT
        if case == "bad_checksum"
        else PublishResponseInvalidReason.UNKNOWN_ENTRY_ID
    )
    assert cleanup_log.call_count == 2
    diagnostic, cleanup = cleanup_log.call_args_list
    assert diagnostic.args == ("outbox_sqs_response_invalid",)
    assert diagnostic.kwargs == {
        "error_reason": error.reason.value,
        "response_field": "body_checksum" if case == "bad_checksum" else "entry_id",
        "event_ids": [str(envelope.event_id)],
    }
    assert cleanup.args == ("outbox_publish_cleanup_failed",)
    assert marker not in str(cleanup_log.call_args_list)
    assert marker not in repr(result)
    assert marker not in str(error)
    assert marker not in str(vars(error.__cause__))
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


def test_client_closes_and_records_diagnostic_before_return(envelope, cleanup_log):
    """送信・終了・診断を完了してから、呼び出し元へ受付結果を返す。"""
    sender, client, _ = _sender()
    trace = []

    def send(**kwargs):
        trace.append("send")
        return {"Successful": [_success(envelope)]}

    def close():
        trace.append("close")
        raise RuntimeError("private body credentials queue-url")

    client.send_message_batch.side_effect = send
    client.close.side_effect = close
    cleanup_log.side_effect = lambda *args, **kwargs: trace.append("diagnostic")
    result = sender.publish_batch([envelope])
    trace.append("returned")

    assert trace == ["send", "close", "diagnostic", "returned"]
    assert result == BatchPublishResult((PublishSucceeded(envelope.event_id),))
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed",
        error_code="publish_cleanup_error",
        original_exception_type="builtins.RuntimeError",
    )
    client.close.assert_called_once()


@pytest.mark.parametrize("logging_failure", [False, True])
def test_invalid_response_diagnostic_is_once_per_sent_batch(
    envelope, logging_failure, cleanup_log
):
    """応答の外部IDを記録せず、診断出力に失敗しても全送信対象の結果を返す。"""
    events = [replace(envelope, event_id=UUID(int=i)) for i in (3, 1)]
    invalid = replace(envelope, event_id=UUID(int=2), event_type="unsupported")
    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    entry = _success(events[0])
    entry["Id"] = marker
    sender, client, _ = _sender(response={"Successful": [entry, _success(events[1])]})
    if logging_failure:
        cleanup_log.side_effect = RuntimeError(marker)

    result = sender.publish_batch([events[0], invalid, events[1]])

    assert [r.event_id for r in result.results] == [
        events[0].event_id,
        invalid.event_id,
        events[1].event_id,
    ]
    assert isinstance(result.results[1].error, PublishEventInvalidError)
    for index in (0, 2):
        assert isinstance(result.results[index].error, PublishResponseInvalidError)
        assert result.results[index].error.reason.value == "unknown_entry_id"
    cleanup_log.assert_called_once_with(
        "outbox_sqs_response_invalid",
        error_reason="unknown_entry_id",
        response_field="entry_id",
        event_ids=[str(event.event_id) for event in events],
    )
    assert marker not in str(cleanup_log.call_args)
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("mismatch", [False, True])
def test_valid_response_shape_does_not_log_response_violation(
    envelope, mismatch, cleanup_log
):
    """正常形式の応答と本文不一致は、応答形式違反の診断を出さない。"""
    entry = _success(envelope)
    if mismatch:
        entry["MD5OfMessageBody"] = "0" * 32
    sender, _, _ = _sender(response={"Successful": [entry]})
    result = sender.publish_batch([envelope])
    assert isinstance(
        result.results[0], PublishFailed if mismatch else PublishSucceeded
    )
    cleanup_log.assert_not_called()


def test_response_diagnostic_process_exit_is_not_swallowed(envelope, cleanup_log):
    """診断出力でのプロセス終了を通常失敗へ変換せず、クライアントは閉じる。"""
    exit_error = KeyboardInterrupt()
    cleanup_log.side_effect = exit_error
    sender, client, _ = _sender(response=None)
    with pytest.raises(KeyboardInterrupt) as caught:
        sender.publish_batch([envelope])
    assert caught.value is exit_error
    client.close.assert_called_once()
