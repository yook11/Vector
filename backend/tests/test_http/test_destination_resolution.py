"""DNS解決結果への宛先方針の適用と、名前解決失敗の区別を検証する。"""

import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.http.destination_policy import HostBlockedError, NotAPublicIpError
from app.http.destination_resolution import HostResolutionError, ensure_host_is_public


def _patch_resolver(*addrs: str | Exception):
    """``_resolve_host`` を patch し、指定の戻り値/例外を返すようにする。"""
    if len(addrs) == 1 and isinstance(addrs[0], Exception):
        return patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(side_effect=addrs[0]),
        )
    return patch(
        "app.http.destination_resolution._resolve_host",
        new=AsyncMock(return_value=list(addrs)),
    )


class TestEnsureHostIsPublic:
    @pytest.mark.asyncio
    async def test_accepts_host_resolving_to_public_ipv4(self) -> None:
        with _patch_resolver("8.8.8.8"):
            addrs = await ensure_host_is_public("dns.google")
        assert len(addrs) == 1
        assert str(addrs[0]) == "8.8.8.8"

    @pytest.mark.asyncio
    async def test_accepts_host_resolving_to_public_ipv6(self) -> None:
        with _patch_resolver("2001:4860:4860::8888"):
            addrs = await ensure_host_is_public("dns.google")
        assert len(addrs) == 1
        assert str(addrs[0]) == "2001:4860:4860::8888"

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_private(self) -> None:
        # docker compose の `backend` のようなサービス名のシナリオ
        with _patch_resolver("172.18.0.5"):
            with pytest.raises(HostBlockedError, match="172.18.0.5"):
                await ensure_host_is_public("backend")

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_link_local(self) -> None:
        # クラウドメタデータエンドポイントのシナリオ (169.254.169.254 への A レコード)
        with _patch_resolver("169.254.169.254"):
            with pytest.raises(HostBlockedError, match="169.254.169.254"):
                await ensure_host_is_public("metadata-attack.example.com")

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_loopback(self) -> None:
        with _patch_resolver("127.0.0.1"):
            with pytest.raises(HostBlockedError, match="127.0.0.1"):
                await ensure_host_is_public("localhost-alias.example.com")

    @pytest.mark.asyncio
    async def test_rejects_when_any_resolved_address_is_private(self) -> None:
        # マルチホーム: public + private が混在 → 全件 public でないと NG
        with _patch_resolver("8.8.8.8", "10.0.0.1"):
            with pytest.raises(HostBlockedError, match="10.0.0.1"):
                await ensure_host_is_public("multihomed.example.com")

    @pytest.mark.asyncio
    async def test_raises_resolution_error_on_dns_failure(self) -> None:
        """DNS失敗は元の例外を原因として保持し、宛先拒否と区別する。"""
        origin = socket.gaierror("Name or service not known")
        with _patch_resolver(origin):
            with pytest.raises(HostResolutionError) as caught:
                await ensure_host_is_public("nonexistent.invalid")
        assert str(caught.value) == (
            "DNS resolution failed for host: nonexistent.invalid: "
            "Name or service not known"
        )
        assert caught.value.__cause__ is origin

    @pytest.mark.asyncio
    async def test_host_rejection_preserves_policy_cause(self) -> None:
        """DNS解決後の宛先拒否は、非公開IP判定の例外を原因として保持する。"""
        with _patch_resolver("10.0.0.1"):
            with pytest.raises(HostBlockedError) as caught:
                await ensure_host_is_public("internal.example")
        assert str(caught.value) == (
            "host resolves to non-public address: internal.example -> 10.0.0.1"
        )
        assert isinstance(caught.value.__cause__, NotAPublicIpError)
