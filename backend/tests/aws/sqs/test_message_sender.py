"""SQSクライアントの作成、1件・まとめての送信結果、送信先の設定不足、SQS通信の終了失敗、外部キャンセルの境界を検証する(クライアントはモック・Stubber・資格情報のない本物のSDK)。"""

import asyncio
import sys
import threading
from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import botocore.session
import pytest
from botocore.credentials import CredentialResolver
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from botocore.stub import Stubber

from app.aws.sqs.errors import MessageSendUnexpectedError
from app.aws.sqs.message_sender import create_sqs_client, open_sqs_message_sender
from app.aws.sqs.outgoing_message import MessageTooLargeError, SqsResponseInvalidError
from app.outbox.event.message import EventMessage
from app.outbox.relay.sender import (
    MessageAccepted,
    MessageSendConfigurationError,
    MessageSendTransportError,
)

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"
# 短い本文で個別・合計サイズの境界へ届くよう、テストだけで使う上限。
LIMIT = 512


@pytest.fixture
def small_message_limit(monkeypatch):
    monkeypatch.setattr("app.aws.sqs.outgoing_message.MAX_MESSAGE_BYTES", LIMIT)


def _accepted(key, body):
    return {
        "Id": str(key),
        "MessageId": f"sqs-{key}",
        "MD5OfMessageBody": md5(
            body.encode("utf-8"), usedforsecurity=False
        ).hexdigest(),
    }


async def _send_batch(client, bodies):
    async with open_sqs_message_sender(
        client_factory=lambda: client, queue_url=QUEUE_URL
    ) as sender:
        results = await sender.send_batch(
            [(key, EventMessage(key, body)) for key, body in bodies.items()]
        )
        return dict(results)


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_client_is_created_with_given_attempts_and_designed_settings(max_attempts):
    """渡した試行回数と、設計で定めたtimeout・proxyとendpointの設定を無視する指定でSQSクライアントを作る。"""
    session = Mock()

    client = create_sqs_client(
        session=session, region="ap-northeast-1", max_attempts=max_attempts
    )

    assert client is session.create_client.return_value
    call = session.create_client.call_args
    config = call.kwargs["config"]
    assert (
        call.args,
        call.kwargs["region_name"],
        config.retries,
        config.connect_timeout,
        config.read_timeout,
        config.proxies,
        config.ignore_configured_endpoint_urls,
    ) == (
        ("sqs",),
        "ap-northeast-1",
        {"mode": "standard", "total_max_attempts": max_attempts},
        3,
        5,
        {},
        True,
    )


@pytest.mark.parametrize("exception_type", [ConnectTimeoutError, ReadTimeoutError])
def test_real_sdk_with_one_attempt_does_not_retry_transport(
    monkeypatch, exception_type
):
    """試行1回で作った本物のSDKは、SQSへの通信の失敗を送り直さず1回で返す。"""
    session = botocore.session.get_session()
    session.set_credentials("testing", "testing", "testing")
    client = create_sqs_client(session=session, region="ap-northeast-1", max_attempts=1)
    send = Mock(side_effect=exception_type(endpoint_url="https://sqs.example"))
    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    try:
        with pytest.raises(exception_type):
            client.send_message_batch(
                QueueUrl=QUEUE_URL, Entries=[{"Id": "event", "MessageBody": "body"}]
            )
        assert send.call_count == 1
    finally:
        client.close()


@pytest.mark.asyncio
async def test_client_creation_failure_becomes_unexpected_send_error():
    """クライアントを作れない想定外の例外は、開く時点で型名だけを持つ想定外の送信の失敗になる。"""
    with pytest.raises(MessageSendUnexpectedError) as caught:
        async with open_sqs_message_sender(
            client_factory=Mock(side_effect=RuntimeError("private")),
            queue_url=QUEUE_URL,
        ):
            pass
    assert (caught.value.exception_type, caught.value.details) == (
        "RuntimeError",
        {"exception_type": "RuntimeError"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 10])
async def test_batch_returns_accepted_in_input_order_with_sent_bodies(count):
    """応答の順によらず、渡した本文をUUIDの文字列をIDにしてそのまま送り、UUIDごとの受付を渡した順で返す。"""
    bodies = {UUID(int=i + 1): f' {{"text": "送信本文{i}"}} \n' for i in range(count)}
    client = botocore.session.get_session().create_client(
        "sqs",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_message_batch",
            {
                "Successful": [
                    _accepted(key, body) for key, body in reversed(bodies.items())
                ],
                "Failed": [],
            },
            {
                "QueueUrl": QUEUE_URL,
                "Entries": [
                    {"Id": str(key), "MessageBody": body}
                    for key, body in bodies.items()
                ],
            },
        )
        results = await _send_batch(client, bodies)
        stubber.assert_no_pending_responses()

    assert list(results.items()) == [
        (key, MessageAccepted(f"sqs-{key}")) for key in bodies
    ]


@pytest.mark.asyncio
async def test_oversized_body_fails_only_its_id_and_the_rest_are_sent(
    small_message_limit,
):
    """上限を超える本文はそのIDだけサイズ超過になり、残りの本文は送って渡した順で結果を返す。"""
    first, oversized, last = UUID(int=1), UUID(int=2), UUID(int=3)
    bodies = {first: "{}", oversized: "x" * (LIMIT + 1), last: "[]"}
    client = Mock()
    client.send_message_batch.return_value = {
        "Successful": [_accepted(last, "[]"), _accepted(first, "{}")]
    }

    results = await _send_batch(client, bodies)

    assert (list(results), type(results[oversized])) == (
        [first, oversized, last],
        MessageTooLargeError,
    )
    assert (results[first], results[last]) == (
        MessageAccepted(f"sqs-{first}"),
        MessageAccepted(f"sqs-{last}"),
    )
    client.send_message_batch.assert_called_once_with(
        QueueUrl=QUEUE_URL,
        Entries=[
            {"Id": str(first), "MessageBody": "{}"},
            {"Id": str(last), "MessageBody": "[]"},
        ],
    )


@pytest.mark.asyncio
async def test_all_oversized_bodies_are_not_sent(small_message_limit):
    """全件が上限を超えるなら、SQSを呼ばずに各IDのサイズ超過を返す。"""
    client = Mock()

    results = await _send_batch(
        client, {UUID(int=1): "x" * (LIMIT + 1), UUID(int=2): "y" * (LIMIT + 1)}
    )

    assert [type(error) for error in results.values()] == [MessageTooLargeError] * 2
    client.send_message_batch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bodies",
    [
        {},
        {UUID(int=i + 1): "{}" for i in range(11)},
        {UUID(int=1): "x" * (LIMIT // 2), UUID(int=2): "y" * (LIMIT // 2 + 1)},
    ],
    ids=["empty", "eleven", "combined_over_limit"],
)
async def test_batch_outside_sqs_limits_is_rejected_before_sending(
    small_message_limit, bodies
):
    """0件・11件・本文の合計が上限超えのまとまりは、SQSを呼ばずに呼び出し側の誤りとして拒否する。"""
    client = Mock()
    with pytest.raises(ValueError):
        await _send_batch(client, bodies)
    client.send_message_batch.assert_not_called()


@pytest.mark.asyncio
async def test_whole_request_failure_applies_to_sent_ids_only(small_message_limit):
    """呼び出し全体の失敗は送ったIDすべての結果になり、送らなかったIDは自分のサイズ超過を保つ。"""
    client = Mock()
    client.send_message_batch.side_effect = ReadTimeoutError(endpoint_url="private")
    first, oversized, last = UUID(int=1), UUID(int=2), UUID(int=3)

    results = await _send_batch(
        client, {first: "{}", oversized: "x" * (LIMIT + 1), last: "[]"}
    )

    assert {key: type(error) for key, error in results.items()} == {
        first: MessageSendTransportError,
        oversized: MessageTooLargeError,
        last: MessageSendTransportError,
    }


@pytest.mark.asyncio
async def test_invalid_response_applies_to_sent_ids_only(small_message_limit):
    """検証できない応答は送ったIDすべての応答不正になり、送らなかったIDは自分のサイズ超過を保つ。"""
    client = Mock()
    client.send_message_batch.return_value = None
    first, oversized, last = UUID(int=1), UUID(int=2), UUID(int=3)

    results = await _send_batch(
        client, {first: "{}", oversized: "x" * (LIMIT + 1), last: "[]"}
    )

    assert {key: type(error) for key, error in results.items()} == {
        first: SqsResponseInvalidError,
        oversized: MessageTooLargeError,
        last: SqsResponseInvalidError,
    }
    assert (results[first].reason, results[first].field) == (
        "invalid_type",
        "response",
    )


@pytest.mark.asyncio
async def test_missing_credentials_fail_at_signing_for_all_ids(monkeypatch):
    """資格情報がなくても開くことはでき、署名で失敗して通信せずに全IDを資格情報がない設定の不足にする。"""
    session = botocore.session.Session()
    session.register_component("credential_provider", CredentialResolver([]))
    created = []

    def create_client():
        client = create_sqs_client(
            session=session, region="ap-northeast-1", max_attempts=1
        )
        monkeypatch.setattr(
            client._endpoint.http_session,
            "send",
            Mock(side_effect=AssertionError("must not send")),
        )
        created.append(client)
        return client

    async with open_sqs_message_sender(
        client_factory=create_client, queue_url=QUEUE_URL
    ) as sender:
        results = await sender.send_batch(
            [
                (UUID(int=1), EventMessage(UUID(int=1), "{}")),
                (UUID(int=2), EventMessage(UUID(int=2), "[]")),
            ]
        )

    assert [(type(error), error.reason) for _, error in results] == [
        (MessageSendConfigurationError, "missing_credentials")
    ] * 2
    assert len(created) == 1


@pytest.mark.asyncio
async def test_blank_queue_url_fails_as_missing_queue_url_before_creating_client():
    """送信先のキューURLが空なら、クライアントを作らずに「キューURLがない」設定の不足として失敗する。"""
    client_factory = Mock()
    with pytest.raises(MessageSendConfigurationError) as caught:
        async with open_sqs_message_sender(
            client_factory=client_factory, queue_url=" "
        ):
            pass
    assert (caught.value.reason, caught.value.details) == (
        "missing_queue_url",
        {"reason": "missing_queue_url"},
    )
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_and_diagnostic_failure_preserve_send_success(monkeypatch):
    """終了処理とその診断が失敗しても、確定した送信成功を失敗に戻さない。"""

    class BrokenOutput:
        def write(self, text):
            raise OSError("private-log")

        def flush(self):
            raise OSError("private-log")

    client = Mock()
    client.send_message.return_value = {
        "MessageId": "id",
        "MD5OfMessageBody": md5(b"{}", usedforsecurity=False).hexdigest(),
    }
    client.close.side_effect = RuntimeError("private-cleanup-error")
    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    async with open_sqs_message_sender(
        client_factory=lambda: client, queue_url="queue"
    ) as sender:
        await sender.send("{}")
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_send_finishes_transport_before_closing_client():
    """キャンセルは送信失敗へ変換せず、実行中の通信を回収してからcloseする。"""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    completed = []

    def send_message(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        completed.append("transport")
        raise RuntimeError("private-transport-error")

    client = Mock(send_message=send_message, close=lambda: completed.append("close"))

    async def send():
        async with open_sqs_message_sender(
            client_factory=lambda: client, queue_url="queue"
        ) as sender:
            await sender.send("{}")

    task = asyncio.create_task(send())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed == ["transport", "close"]
