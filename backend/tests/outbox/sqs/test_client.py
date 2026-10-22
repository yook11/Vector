"""SQSクライアントを、資格情報を固定せず単一試行の設定で作る境界を検証する(セッションはモックか本物のSDK)。"""

from unittest.mock import Mock

import botocore.session
import pytest
from botocore.exceptions import ConnectTimeoutError, NoRegionError, ReadTimeoutError

from app.outbox.sqs.client import create_sqs_client


def test_client_is_created_with_one_attempt_and_designed_timeouts() -> None:
    """資格情報を自分で取得せず、試行1回と設計で定めた接続・応答待ちtimeoutでSQSクライアントを作る。"""
    session = Mock()

    client = create_sqs_client(session=session, region="ap-northeast-1")

    assert client is session.create_client.return_value
    call = session.create_client.call_args
    config = call.kwargs["config"]
    assert (
        call.args,
        set(call.kwargs),
        call.kwargs["region_name"],
        config.retries,
        config.connect_timeout,
        config.read_timeout,
    ) == (
        ("sqs",),
        {"region_name", "config"},
        "ap-northeast-1",
        {"mode": "standard", "total_max_attempts": 1},
        3,
        5,
    )
    session.get_credentials.assert_not_called()


def test_missing_region_is_rejected_before_creating_client() -> None:
    """リージョンが空なら、クライアントを作る前にリージョンがないSDK例外を送出する。"""
    session = Mock()
    with pytest.raises(NoRegionError):
        create_sqs_client(session=session, region="")
    session.create_client.assert_not_called()


@pytest.mark.parametrize("exception_type", [ConnectTimeoutError, ReadTimeoutError])
def test_real_sdk_does_not_retry_sqs_transport(monkeypatch, exception_type) -> None:
    """本物のSDKでも、SQSへの通信の失敗を送り直さず1回で返す。"""
    session = botocore.session.get_session()
    session.set_credentials("testing", "testing", "testing")
    client = create_sqs_client(session=session, region="ap-northeast-1")
    send = Mock(side_effect=exception_type(endpoint_url="https://sqs.example"))
    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    try:
        with pytest.raises(exception_type):
            client.send_message_batch(
                QueueUrl="https://sqs.ap-northeast-1.amazonaws.com/123456789012/test",
                Entries=[{"Id": "event", "MessageBody": "body"}],
            )
        assert send.call_count == 1
    finally:
        client.close()
