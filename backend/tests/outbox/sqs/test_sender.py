"""バッチの受付結果と、個別失敗・全体失敗の境界を検証する(SQSクライアントはモック・Stubber・資格情報のない本物のSDK)。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import botocore.session
import pytest
from botocore.credentials import CredentialResolver
from botocore.exceptions import (
    ClientError,
    HTTPClientError,
    NoCredentialsError,
    ReadTimeoutError,
)
from botocore.stub import Stubber

from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendCredentialsError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
)
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseInvalidError,
)
from app.outbox.delivery.repository import LeasedOutboxEvent
from app.outbox.publishing.contracts import (
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.route import EventMessage
from app.outbox.sqs import failure_handler as failure_handler_module
from app.outbox.sqs.failure_handler import SqsPublishFailureHandler
from app.outbox.sqs.sender import SqsSender

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"


LIMIT = 512


@pytest.fixture
def envelope():
    return EventMessage(UUID(int=1), ' {"text": "送信本文"} \n')


@pytest.fixture
def cleanup_log(monkeypatch):
    log = Mock()
    monkeypatch.setattr(failure_handler_module.logger, "warning", log)
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


def _body(envelope):
    return envelope.body


def _sender(*, response=None, error=None, close_error=None):
    client = Mock()
    client.send_message_batch.return_value = response
    client.send_message_batch.side_effect = error
    client.close.side_effect = close_error
    factory = Mock(return_value=client)
    return (
        SqsSender(
            failure_handler=SqsPublishFailureHandler(),
            client_factory=factory,
        ),
        client,
        factory,
    )


@pytest.fixture
def small_message_limit(monkeypatch):
    """短い本文で個別・合計サイズの境界へ到達できる上限を使う。"""
    monkeypatch.setattr("app.aws.sqs.outgoing_message.MAX_MESSAGE_BYTES", LIMIT)


def _sized(envelope, size):
    return replace(envelope, body="x" * size)


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
    sender = SqsSender(
        failure_handler=SqsPublishFailureHandler(),
        client_factory=lambda: client,
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
                assert sender.send_batch(
                    queue_url=QUEUE_URL,
                    send_requests=[(message, message) for message in events],
                ) == tuple((e, PublishSucceeded(e.event_id)) for e in events)
            stubber.assert_no_pending_responses()
    finally:
        client.close()
    assert events == original


def test_partial_failure_keeps_success_and_rejects_only_failed_entry(envelope):
    """HTTP 200の部分失敗では失敗した項目だけをSQSの拒否にし、成功の受付は保つ。"""
    second = replace(envelope, event_id=UUID(int=2))
    sender, client, _ = _sender(
        response={
            "Successful": [_success(second)],
            "Failed": [_failure(envelope.event_id)],
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-id"},
        }
    )
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope), (second, second)]
    )
    assert [item.event_id for _, item in result] == [
        envelope.event_id,
        second.event_id,
    ]
    (_, failed), (_, succeeded) = result
    assert isinstance(failed, PublishFailed)
    assert (type(failed.error), failed.error.reason, failed.error.service_code) == (
        SqsServiceError,
        "access_denied",
        "AccessDenied",
    )
    assert succeeded == PublishSucceeded(second.event_id)
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


def test_mixed_results_return_original_leased_events(small_message_limit):
    """応答順と送信前の除外によらず、元の確保済みイベントを対応する結果と同一参照で返す。"""
    leased_events = [
        LeasedOutboxEvent(
            envelope=EventEnvelope(
                event_id=UUID(int=i),
                event_type="test.event",
                schema_version=1,
                occurred_at=datetime(2026, 10, 23, tzinfo=UTC),
                payload={"index": i},
            ),
            lease_token=UUID(int=100 + i),
            attempt_count=i,
        )
        for i in range(1, 5)
    ]
    messages = [EventMessage(event.envelope.event_id, "{}") for event in leased_events]
    messages[1] = replace(messages[1], body="x" * (LIMIT + 1))
    send_requests = list(zip(leased_events, messages, strict=True))
    sender, _, _ = _sender(
        response={
            "Successful": [_success(messages[3]), _success(messages[0])],
            "Failed": [_failure(messages[2].event_id)],
        }
    )

    results = sender.send_batch(queue_url=QUEUE_URL, send_requests=send_requests)

    assert len(results) == len(leased_events)
    for original, (returned, outcome) in zip(leased_events, results, strict=True):
        assert returned is original
        assert outcome.event_id == original.envelope.event_id
    assert isinstance(results[0][1], PublishSucceeded)
    assert isinstance(results[1][1].error, MessageTooLargeError)
    assert isinstance(results[2][1].error, SqsServiceError)
    assert isinstance(results[3][1], PublishSucceeded)


def test_original_target_is_retained_when_request_list_changes(envelope):
    """送信中に呼び出し元のリストが変更されても、受け取った対象との対応を維持する。"""
    original = object()
    send_requests = [(original, envelope)]
    sender, client, _ = _sender()

    def send(**kwargs):
        send_requests.clear()
        return {"Successful": [_success(envelope)]}

    client.send_message_batch.side_effect = send
    results = sender.send_batch(queue_url=QUEUE_URL, send_requests=send_requests)

    assert len(results) == 1
    assert results[0][0] is original
    assert results[0][1] == PublishSucceeded(envelope.event_id)


@pytest.mark.parametrize("send_requests", [None, "private", b"private", iter(())])
def test_non_sequence_requests_are_rejected_before_client_creation(send_requests):
    """送信要求はSequenceとして受け取り、型違反ではクライアントを生成しない。"""
    sender, _, factory = _sender()
    with pytest.raises(TypeError, match="send requests must be a sequence"):
        sender.send_batch(queue_url=QUEUE_URL, send_requests=send_requests)
    factory.assert_not_called()


@pytest.mark.parametrize("sender_fault", [False, True])
def test_unknown_entry_code_is_unknown_regardless_of_sender_fault(
    envelope, sender_fault
):
    """未知のAWSコードを、SenderFaultによらず未知の理由のSQSの拒否として保持する。"""
    sender, _, _ = _sender(
        response={"Failed": [_failure(envelope.event_id, "FutureCode", sender_fault)]}
    )
    error = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )[0][1].error
    assert (type(error), error.reason, error.service_code) == (
        SqsServiceError,
        "unknown",
        "FutureCode",
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
            sender.send_batch(
                queue_url=QUEUE_URL,
                send_requests=[(message, message) for message in events],
            )
        factory.assert_not_called()
    elif extra:
        assert isinstance(
            sender.send_batch(
                queue_url=QUEUE_URL,
                send_requests=[(message, message) for message in events],
            )[0][1].error,
            MessageTooLargeError,
        )
        factory.assert_not_called()
    else:
        assert all(
            isinstance(r, PublishSucceeded)
            for _, r in sender.send_batch(
                queue_url=QUEUE_URL,
                send_requests=[(message, message) for message in events],
            )
        )
        client.send_message_batch.assert_called_once()


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "private"}},
                "SendMessageBatch",
            ),
            SqsServiceError,
        ),
        (ReadTimeoutError(endpoint_url="private"), MessageSendTransportError),
        (HTTPClientError(error="private"), MessageSendTransportError),
        (
            ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"),
            MessageSendCredentialsError,
        ),
        (NoCredentialsError(), MessageSendConfigurationError),
        (RuntimeError("private"), MessageSendUnexpectedError),
    ],
)
@pytest.mark.parametrize("close_fails", [False, True])
def test_whole_request_failure_preserves_local_failure_and_cleanup(
    envelope, exc, expected, close_fails, cleanup_log
):
    """全体失敗(署名時の資格情報の取得の失敗を含む)を送信対象へ反映し、ローカルの不正や先行障害を上書きしない。"""
    cleanup_log.side_effect = RuntimeError("private log failure")
    good = replace(envelope, event_id=UUID(int=2))
    bad = replace(envelope, event_id=UUID(int=3), body="x" * (1024 * 1024 + 1))
    sender, client, _ = _sender(
        error=exc, close_error=ValueError("private") if close_fails else None
    )
    result = sender.send_batch(
        queue_url=QUEUE_URL,
        send_requests=[(envelope, envelope), (good, good), (bad, bad)],
    )
    assert [item.event_id for _, item in result] == [
        envelope.event_id,
        good.event_id,
        bad.event_id,
    ]
    for _, outcome in result[:2]:
        assert type(outcome.error) is expected
    assert isinstance(result[2][1].error, MessageTooLargeError)
    assert cleanup_log.call_count == int(close_fails)
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("logging_failure", [False, True])
def test_cleanup_failure_preserves_success(envelope, logging_failure, cleanup_log):
    """終了失敗を例外の型だけで記録し、診断出力が失敗しても受付結果を返す。"""
    if logging_failure:
        cleanup_log.side_effect = RuntimeError("private log failure")
    sender, _, _ = _sender(
        response={"Successful": [_success(envelope)]},
        close_error=RuntimeError("private"),
    )
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )
    assert result == ((envelope, PublishSucceeded(envelope.event_id)),)
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed", error_class="builtins.RuntimeError"
    )


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
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope), (second, second)]
    )
    assert [item.event_id for _, item in result] == [
        envelope.event_id,
        second.event_id,
    ]
    for _, outcome in result:
        expected = {
            "root": ("invalid_type", "response"),
            "list": ("invalid_type", "failed_entries"),
            "entry": ("invalid_type", "failed_entry"),
            "missing": ("not_returned", "entry_id"),
            "unknown": ("not_in_request", "entry_id"),
            "duplicate": ("duplicate", "entry_id"),
            "overlap": ("duplicate", "entry_id"),
            "message_id": ("empty", "message_id"),
            "md5": ("missing", "body_checksum"),
            "code": ("invalid_type", "error_code"),
            "sender_fault": ("invalid_type", "sender_fault"),
        }
        error = outcome.error
        assert (type(error), (error.reason.value, error.field.value)) == (
            SqsResponseInvalidError,
            expected[case],
        )
    client.close.assert_called_once()


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_process_exit_is_not_wrapped_or_replaced_by_cleanup(envelope, exc, cleanup_log):
    """プロセス終了を分類せず、通常の終了障害でも置き換えない。"""
    cleanup_log.side_effect = RuntimeError("private log failure")
    sender, client, _ = _sender(error=exc, close_error=ValueError("cleanup"))
    with pytest.raises(type(exc)) as caught:
        sender.send_batch(queue_url=QUEUE_URL, send_requests=[(envelope, envelope)])
    assert caught.value is exc
    client.close.assert_called_once()


def test_each_batch_creates_and_closes_its_own_client(envelope):
    """資格情報を自分で固定せず、送信ごとにクライアントを作って、その送信のクライアントを終了する。"""
    session = Mock()
    clients = [Mock(), Mock()]
    for client in clients:
        client.send_message_batch.return_value = {"Successful": [_success(envelope)]}
    session.create_client.side_effect = clients
    sender = SqsSender.from_session(
        session=session,
        region="ap-northeast-1",
    )
    for _ in range(2):
        assert sender.send_batch(
            queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
        ) == ((envelope, PublishSucceeded(envelope.event_id)),)
    assert session.create_client.call_count == 2
    session.get_credentials.assert_not_called()
    assert (
        clients[0].send_message_batch.call_args
        == clients[1].send_message_batch.call_args
    )
    for client in clients:
        client.send_message_batch.assert_called_once()
        client.close.assert_called_once()


def test_missing_credentials_fail_at_signing_without_sending(envelope, monkeypatch):
    """資格情報が見つからなくてもクライアントは作れ、署名で失敗して通信せずに全件を資格情報がない設定の不足にし、クライアントを閉じる。"""
    session = botocore.session.Session()
    session.register_component("credential_provider", CredentialResolver([]))
    created = []
    create_client = session.create_client

    def create_and_watch(*args, **kwargs):
        client = create_client(*args, **kwargs)
        monkeypatch.setattr(
            client._endpoint.http_session,
            "send",
            Mock(side_effect=AssertionError("must not send")),
        )
        monkeypatch.setattr(client, "close", Mock(wraps=client.close))
        created.append(client)
        return client

    monkeypatch.setattr(session, "create_client", create_and_watch)
    sender = SqsSender.from_session(session=session, region="ap-northeast-1")

    error = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )[0][1].error

    assert (type(error), error.reason) == (
        MessageSendConfigurationError,
        "missing_credentials",
    )
    created[0].close.assert_called_once()


def test_unexpected_client_creation_failure_keeps_only_its_type(envelope):
    """想定外のクライアント生成の失敗は、型名だけを持つ想定外の送信の失敗になる。"""
    sender, _, factory = _sender()
    factory.side_effect = RuntimeError("private")
    error = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )[0][1].error
    assert (type(error), error.exception_type) == (
        MessageSendUnexpectedError,
        "RuntimeError",
    )


def test_results_are_frozen_and_do_not_expose_diagnostics(envelope, cleanup_log):
    """送信結果を書き換えられず、表示に診断情報を漏らさない。"""
    marker = "PRIVATE_BODY_CREDENTIAL_QUEUE"
    sender, _, _ = _sender(
        response={
            "Failed": [_failure(envelope.event_id, marker)],
            "ResponseMetadata": {"RequestId": marker},
        },
        close_error=RuntimeError(marker),
    )
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )
    assert marker not in repr(result)
    assert marker not in str(result[0][1].error)
    assert marker not in str(cleanup_log.call_args)
    for obj, attr, value in [
        (result[0][1], "event_id", UUID(int=2)),
        (PublishSucceeded(envelope.event_id), "event_id", UUID(int=2)),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, value)


def test_oversized_event_stops_without_configuration_alarm(
    envelope, small_message_limit
):
    """サイズ超過を再試行対象外とし、設定修復アラームにしない。"""
    from app.aws.sqs.configuration_fix import requires_configuration_fix
    from app.outbox.delivery.retry_policy import (
        NonRetryable,
        NonRetryableReason,
        decide_publish_retry,
    )

    sender, _, _ = _sender()
    error = sender.send_batch(
        queue_url=QUEUE_URL,
        send_requests=[(_sized(envelope, LIMIT + 1), _sized(envelope, LIMIT + 1))],
    )[0][1].error
    assert decide_publish_retry(error, attempt_count=1, jitter=0.5) == NonRetryable(
        NonRetryableReason.NON_RETRYABLE_FAILURE
    )
    assert not requires_configuration_fix(error)


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
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope), (second, second)]
    )
    assert [r.event_id for _, r in result] == [envelope.event_id, second.event_id]
    assert [(type(r.error), r.error.reason) for _, r in result] == [
        (SqsServiceError, "access_denied"),
        (SqsServiceError, "throttled"),
    ]


def test_initialization_failure_preserves_oversized_event(envelope):
    """クライアントを作れなくても、送る前に分かったサイズ超過は設定の不足に置き換えない。"""
    bad = replace(envelope, event_id=UUID(int=2), body="x" * (1024 * 1024 + 1))
    sender, _, factory = _sender()
    factory.side_effect = NoCredentialsError()
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(bad, bad), (envelope, envelope)]
    )
    assert [item.event_id for _, item in result] == [
        bad.event_id,
        envelope.event_id,
    ]
    assert isinstance(result[0][1].error, MessageTooLargeError)
    error = result[1][1].error
    assert (type(error), error.reason) == (
        MessageSendConfigurationError,
        "missing_credentials",
    )


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
    envelope = EventMessage(envelope.event_id, body)
    assert sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    ) == ((envelope, PublishSucceeded(envelope.event_id)),)
    assert client.send_message_batch.call_args.kwargs["Entries"] == [
        {"Id": str(envelope.event_id), "MessageBody": body}
    ]


def test_mixed_integrity_and_service_failures_keep_success_and_cleanup(
    envelope, cleanup_log
):
    """本文不一致・サービス拒否・終了障害が混在しても成功を上書きしない。"""
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
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(message, message) for message in events]
    )
    assert [r.event_id for _, r in result] == [e.event_id for e in events]
    assert result[0][1] == PublishSucceeded(events[0].event_id)
    assert isinstance(result[1][1].error, MessageBodyMismatchError)
    assert (type(result[2][1].error), result[2][1].error.reason) == (
        SqsServiceError,
        "throttled",
    )
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed", error_class="builtins.RuntimeError"
    )
    client.send_message_batch.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("case", ["bad_checksum", "unknown_id"])
def test_response_invalid_preserves_local_failure_and_cleanup_without_raw_values(
    envelope, case, cleanup_log
):
    """応答違反を全送信対象へ反映し、送信前の不正と終了失敗を上書きしない。"""
    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    invalid = replace(envelope, event_id=UUID(int=2), body="x" * (1024 * 1024 + 1))
    entry = _success(envelope)
    entry["MD5OfMessageBody" if case == "bad_checksum" else "Id"] = marker
    response = {"Successful": [entry], "ResponseMetadata": {"RequestId": marker}}
    sender, client, _ = _sender(response=response, close_error=RuntimeError(marker))
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(invalid, invalid), (envelope, envelope)]
    )
    assert [item.event_id for _, item in result] == [
        invalid.event_id,
        envelope.event_id,
    ]
    assert isinstance(result[0][1].error, MessageTooLargeError)
    error = result[1][1].error
    assert isinstance(error, SqsResponseInvalidError)
    assert cleanup_log.call_count == 2
    diagnostic, cleanup = cleanup_log.call_args_list
    assert diagnostic.args == ("outbox_sqs_response_invalid",)
    assert diagnostic.kwargs == {
        "error_reason": "invalid_format"
        if case == "bad_checksum"
        else "not_in_request",
        "response_field": "body_checksum" if case == "bad_checksum" else "entry_id",
        "event_ids": [str(envelope.event_id)],
    }
    assert cleanup.args == ("outbox_publish_cleanup_failed",)
    assert marker not in str(cleanup_log.call_args_list)
    assert marker not in repr(result)
    assert marker not in str(error)
    assert marker not in str(vars(error))
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
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )
    trace.append("returned")

    assert trace == ["send", "close", "diagnostic", "returned"]
    assert result == ((envelope, PublishSucceeded(envelope.event_id)),)
    cleanup_log.assert_called_once_with(
        "outbox_publish_cleanup_failed", error_class="builtins.RuntimeError"
    )
    client.close.assert_called_once()


@pytest.mark.parametrize("logging_failure", [False, True])
def test_invalid_response_diagnostic_is_once_per_sent_batch(
    envelope, logging_failure, cleanup_log
):
    """応答の外部IDを記録せず、診断出力に失敗しても全送信対象の結果を返す。"""
    events = [replace(envelope, event_id=UUID(int=i)) for i in (3, 1)]
    invalid = replace(envelope, event_id=UUID(int=2), body="x" * (1024 * 1024 + 1))
    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    entry = _success(events[0])
    entry["Id"] = marker
    sender, client, _ = _sender(response={"Successful": [entry, _success(events[1])]})
    if logging_failure:
        cleanup_log.side_effect = RuntimeError(marker)

    result = sender.send_batch(
        queue_url=QUEUE_URL,
        send_requests=[
            (events[0], events[0]),
            (invalid, invalid),
            (events[1], events[1]),
        ],
    )

    assert [r.event_id for _, r in result] == [
        events[0].event_id,
        invalid.event_id,
        events[1].event_id,
    ]
    assert isinstance(result[1][1].error, MessageTooLargeError)
    for index in (0, 2):
        assert isinstance(result[index][1].error, SqsResponseInvalidError)
    cleanup_log.assert_called_once_with(
        "outbox_sqs_response_invalid",
        error_reason="not_in_request",
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
    result = sender.send_batch(
        queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
    )
    assert isinstance(result[0][1], PublishFailed if mismatch else PublishSucceeded)
    cleanup_log.assert_not_called()


def test_response_diagnostic_process_exit_is_not_swallowed(envelope, cleanup_log):
    """診断出力でのプロセス終了を通常失敗へ変換せず、クライアントは閉じる。"""
    exit_error = KeyboardInterrupt()
    cleanup_log.side_effect = exit_error
    sender, client, _ = _sender(response=None)
    with pytest.raises(KeyboardInterrupt) as caught:
        sender.send_batch(queue_url=QUEUE_URL, send_requests=[(envelope, envelope)])
    assert caught.value is exit_error
    client.close.assert_called_once()


def test_publisher_passes_diagnostics_to_injected_failure_handler(envelope):
    """送信対象のIDと応答の不正、元のままの終了障害を、注入されたハンドラーへ渡す。"""
    handler = Mock(spec=SqsPublishFailureHandler)
    client = Mock()
    client.send_message_batch.return_value = None
    close_error = RuntimeError("private cleanup")
    client.close.side_effect = close_error
    publisher = SqsSender(
        client_factory=lambda: client,
        failure_handler=handler,
    )
    invalid = replace(envelope, event_id=UUID(int=2), body="x" * (1024 * 1024 + 1))

    result = publisher.send_batch(
        queue_url=QUEUE_URL, send_requests=[(invalid, invalid), (envelope, envelope)]
    )

    assert [outcome.event_id for _, outcome in result] == [
        invalid.event_id,
        envelope.event_id,
    ]
    assert isinstance(result[0][1].error, MessageTooLargeError)
    assert isinstance(result[1][1].error, SqsResponseInvalidError)
    handler.handle_invalid_response.assert_called_once()
    diagnostic = handler.handle_invalid_response.call_args.kwargs
    assert diagnostic["event_ids"] == (envelope.event_id,)
    assert diagnostic["error"] is result[1][1].error
    handler.handle_cleanup_failure.assert_called_once_with(close_error)


@pytest.mark.parametrize("primary_error", [None, KeyboardInterrupt()])
def test_cleanup_diagnostic_failure_preserves_original_outcome(envelope, primary_error):
    """終了診断そのものが失敗しても、受付成功と先行するプロセス終了を維持する。"""
    handler = Mock(spec=SqsPublishFailureHandler)
    handler.handle_cleanup_failure.side_effect = ValueError("private diagnostic")
    client = Mock()
    client.send_message_batch.return_value = {"Successful": [_success(envelope)]}
    client.send_message_batch.side_effect = primary_error
    client.close.side_effect = RuntimeError("private cleanup")
    sender = SqsSender(client_factory=lambda: client, failure_handler=handler)
    if primary_error is None:
        assert sender.send_batch(
            queue_url=QUEUE_URL, send_requests=[(envelope, envelope)]
        ) == ((envelope, PublishSucceeded(envelope.event_id)),)
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            sender.send_batch(queue_url=QUEUE_URL, send_requests=[(envelope, envelope)])
        assert caught.value is primary_error
    client.close.assert_called_once()
