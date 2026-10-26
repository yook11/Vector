"""可視性変更用クライアントの通信設定を、SDKセッションのモックで確認する。"""

from unittest.mock import Mock

from app.aws.sqs.visibility import create_sqs_visibility_client


def test_visibility_client_preserves_transport_settings():
    """可視性変更の時間予算と接続先保護を維持したクライアントを生成する。"""
    session = Mock()

    client = create_sqs_visibility_client(session=session, region="ap-northeast-1")

    assert client is session.create_client.return_value
    session.create_client.assert_called_once()
    call = session.create_client.call_args
    config = call.kwargs["config"]
    assert (
        call.args,
        call.kwargs["region_name"],
        config.connect_timeout,
        config.read_timeout,
        config.retries,
        config.proxies,
        config.ignore_configured_endpoint_urls,
    ) == (
        ("sqs",),
        "ap-northeast-1",
        5,
        5,
        {"mode": "standard", "total_max_attempts": 1},
        {},
        True,
    )
