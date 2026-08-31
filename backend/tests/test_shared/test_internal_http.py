"""``make_internal_async_client`` のユニットテスト。

内部宛の client が満たすべき構造的性質を固定する: egress proxy を経由しないこと、
redirect を追わないこと、timeout が届くこと。実 HTTP は出さない。
"""

from __future__ import annotations

import httpx
import pytest

from app.shared.http.internal import make_internal_async_client


class TestEgressProxyIsNotUsed:
    """env に proxy が居ても内部宛の経路には載らない。

    ``NO_PROXY`` に内部 host を書き忘れても迂回しない、が満たすべき性質。
    設定値ではなく **request が実際に載る transport** で確認する。
    """

    @pytest.mark.asyncio
    async def test_env_proxy_is_not_mounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.vector.internal:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.vector.internal:3128")

        async with make_internal_async_client(timeout=5.0) as client:
            assert client._mounts == {}
            for url in ("https://internal.example/x", "http://internal.example/x"):
                assert client._transport_for_url(httpx.URL(url)) is client._transport

    @pytest.mark.asyncio
    async def test_env_proxy_would_be_mounted_without_the_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上のテストが httpx の既定を確認しているだけではないことの対照。"""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.vector.internal:3128")

        async with httpx.AsyncClient(timeout=5.0) as client:  # noqa: TID251
            assert client._mounts != {}


class TestClientShape:
    @pytest.mark.asyncio
    async def test_does_not_follow_redirects(self) -> None:
        async with make_internal_async_client(timeout=5.0) as client:
            assert client.follow_redirects is False

    @pytest.mark.asyncio
    async def test_timeout_is_applied(self) -> None:
        async with make_internal_async_client(timeout=1.5) as client:
            assert client.timeout == httpx.Timeout(1.5)
