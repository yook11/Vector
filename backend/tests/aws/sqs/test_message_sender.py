"""送信先の設定不足、SQS通信の終了失敗、外部キャンセルの境界を検証する(クライアントはモック)。"""

import asyncio
import sys
import threading
from hashlib import md5
from unittest.mock import Mock

import pytest

from app.aws.sqs.errors import MessageSendConfigurationError
from app.aws.sqs.message_sender import open_sqs_message_sender


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
