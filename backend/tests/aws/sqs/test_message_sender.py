"""SQS通信の終了失敗と外部キャンセルの境界を検証する。"""

import asyncio
import threading
from hashlib import md5
from unittest.mock import Mock

import pytest

from app.aws.sqs.message_sender import open_sqs_message_sender


@pytest.mark.asyncio
async def test_cleanup_and_diagnostic_failure_preserve_send_success(monkeypatch):
    """終了処理とその診断が失敗しても、確定した送信成功を失敗に戻さない。"""
    from app.aws.sqs import message_sender as sqs_sender

    client = Mock()
    client.send_message.return_value = {
        "MessageId": "id",
        "MD5OfMessageBody": md5(b"{}", usedforsecurity=False).hexdigest(),
    }
    client.close.side_effect = RuntimeError("private-cleanup-error")
    monkeypatch.setattr(
        sqs_sender, "logger", Mock(warning=Mock(side_effect=RuntimeError))
    )
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
