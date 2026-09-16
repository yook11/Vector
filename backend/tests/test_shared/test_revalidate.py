"""FrontendRevalidateNotifier — HTTP 200 / HTTP error / network error の 3 ケース。

設計契約: 通常の通知失敗は warn に降格し、実行キャンセルは伝播する。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx  # noqa: TID251 (テスト内 mock 構築のため、実通信なし)
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.shared import revalidate
from app.shared.revalidate import (
    FrontendRevalidateNotifier,
    NullRevalidateNotifier,
)


def _notifier(*, secret_provider=None) -> FrontendRevalidateNotifier:
    return FrontendRevalidateNotifier(
        frontend_base_url="http://frontend:3000",
        secret_provider=secret_provider
        or AsyncMock(return_value=SecretStr("test-secret-32characters-long-xxxx")),
    )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestNotify:
    @pytest.mark.asyncio
    async def test_posts_given_tags_to_revalidate_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.read()
            return httpx.Response(200, json={"ok": True})

        _patch_transport(monkeypatch, httpx.MockTransport(handler))

        await _notifier().notify(tags=["trends", "briefing:list"])

        assert captured["url"] == "http://frontend:3000/api/internal/revalidate"
        assert (
            captured["headers"]["authorization"]
            == "Bearer test-secret-32characters-long-xxxx"
        )
        assert captured["body"].decode() == '{"tags":["trends","briefing:list"]}'

    @pytest.mark.asyncio
    async def test_does_not_raise_on_http_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _patch_transport(monkeypatch, httpx.MockTransport(handler))

        # 例外は出ない (warn 降格)
        await _notifier().notify(tags=["trends"])

    @pytest.mark.asyncio
    async def test_does_not_raise_on_network_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _patch_transport(monkeypatch, httpx.MockTransport(handler))

        await _notifier().notify(tags=["trends"])


class TestNullNotifier:
    @pytest.mark.asyncio
    async def test_is_no_op(self) -> None:
        # 何もしないし raise もしない
        await NullRevalidateNotifier().notify(tags=["trends"])


@pytest.mark.asyncio
async def test_secret_failure_logs_only_error_class(monkeypatch):
    """キー取得失敗の警告に例外自由文や秘密値を含めない。"""
    provider = AsyncMock(side_effect=RuntimeError("private-notification-secret"))
    client = Mock()
    monkeypatch.setattr(revalidate, "make_internal_async_client", client)

    with capture_logs() as logs:
        await _notifier(secret_provider=provider).notify(tags=["articles:list"])

    client.assert_not_called()
    assert logs == [
        {
            "event": "frontend_revalidate_failed",
            "tags": ["articles:list"],
            "error_class": "builtins.RuntimeError",
            "log_level": "warning",
        }
    ]


@pytest.mark.asyncio
async def test_notification_diagnostic_failure_does_not_escape(monkeypatch):
    """通知障害のログ出力が失敗しても呼び出し元へ例外を返さない。"""
    provider = AsyncMock(side_effect=RuntimeError("notification unavailable"))
    log = Mock(warning=Mock(side_effect=RuntimeError("logging failed")))
    monkeypatch.setattr(revalidate, "logger", log)

    await _notifier(secret_provider=provider).notify(tags=["articles:list"])

    log.warning.assert_called_once()


@pytest.mark.asyncio
async def test_notification_cancellation_propagates():
    """実行キャンセルを通常の通知障害として抑止しない。"""
    provider = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _notifier(secret_provider=provider).notify(tags=["articles:list"])
