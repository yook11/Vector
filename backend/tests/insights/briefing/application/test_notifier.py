"""FrontendRevalidateNotifier — HTTP 200 / HTTP error / network error の 3 ケース。

設計契約: notify は **絶対に raise しない** (warn 降格のみ)。
"""

from __future__ import annotations

import httpx  # noqa: TID251 (テスト内 mock 構築のため、実通信なし)
import pytest

from app.insights.briefing.application.notifier import (
    FrontendRevalidateNotifier,
    NullBriefingNotifier,
)


def _notifier() -> FrontendRevalidateNotifier:
    return FrontendRevalidateNotifier(
        frontend_base_url="http://frontend:3000",
        secret="test-secret-32characters-long-xxxx",
    )


class TestNotify:
    @pytest.mark.asyncio
    async def test_posts_correct_payload_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.read()
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)

        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        notifier = _notifier()
        await notifier.notify(category_slug="ai")

        assert captured["url"] == "http://frontend:3000/api/internal/revalidate"
        assert (
            captured["headers"]["authorization"]
            == "Bearer test-secret-32characters-long-xxxx"
        )
        body = captured["body"].decode()
        assert "briefing:ai" in body
        assert "briefing:list" in body

    @pytest.mark.asyncio
    async def test_does_not_raise_on_http_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        notifier = _notifier()
        # 例外は出ない (warn 降格)
        await notifier.notify(category_slug="ai")

    @pytest.mark.asyncio
    async def test_does_not_raise_on_network_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        notifier = _notifier()
        await notifier.notify(category_slug="ai")


class TestNullNotifier:
    @pytest.mark.asyncio
    async def test_is_no_op(self) -> None:
        # 何もしないし raise もしない
        await NullBriefingNotifier().notify(category_slug="ai")
