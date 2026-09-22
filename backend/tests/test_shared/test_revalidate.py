"""FrontendRevalidateNotifier — HTTP 200 / HTTP error / network error の 3 ケース。

設計契約: 通常の通知失敗は warn に降格し、実行キャンセルは伝播する。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx  # noqa: TID251 (テスト内 mock 構築のため、実通信なし)
import pytest
import structlog
from pydantic import SecretStr

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
async def test_secret_failure_logs_only_error_class(monkeypatch, capsys):
    """キー取得失敗は通知ポリシーで処理箇所と型を残し、秘密値は出さない。"""
    provider = AsyncMock(side_effect=RuntimeError("PRIVATE_NOTIFICATION_SECRET"))
    client = Mock()
    monkeypatch.setattr(revalidate, "make_internal_async_client", client)

    with structlog.contextvars.bound_contextvars(request_id="request-001"):
        await _notifier(secret_provider=provider).notify(tags=["articles:list"])

    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["log_policy"] == "cache_revalidation"
    assert record["operation"] == "get_secret"
    assert record["error_class"] == "builtins.RuntimeError"
    assert record["request_id"] == "request-001"
    assert record["tags"] == ["articles:list"]
    assert "PRIVATE_NOTIFICATION_SECRET" not in output
    assert "error_message" not in record


@pytest.mark.asyncio
async def test_http_failure_log_identifies_notification_operation(monkeypatch, capsys):
    """HTTP失敗の記録は秘密取得と区別し、応答本文や内部URLを出さない。"""

    async def respond(request):
        return httpx.Response(500, text="PRIVATE_RESPONSE")

    _patch_transport(monkeypatch, httpx.MockTransport(respond))
    await _notifier().notify(tags=["articles:list"])

    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["event"] == "frontend_revalidate_failed"
    assert record["operation"] == "notify"
    assert record["error_class"] == "httpx.HTTPStatusError"
    assert record["level"] == "warning"
    assert "PRIVATE_RESPONSE" not in output
    assert "http://frontend:3000" not in output


@pytest.mark.asyncio
async def test_success_log_uses_cache_revalidation_policy(monkeypatch, capsys):
    """成功時も通知専用ポリシーで更新対象をJSONへ記録する。"""

    async def respond(request):
        return httpx.Response(200)

    _patch_transport(monkeypatch, httpx.MockTransport(respond))
    await _notifier().notify(tags=["articles:list"])

    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "frontend_revalidate_ok"
    assert record["log_policy"] == "cache_revalidation"
    assert record["tags"] == ["articles:list"]
    assert record["level"] == "info"


@pytest.mark.asyncio
async def test_notification_cancellation_propagates():
    """実行キャンセルを通常の通知障害として抑止しない。"""
    provider = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _notifier(secret_provider=provider).notify(tags=["articles:list"])
