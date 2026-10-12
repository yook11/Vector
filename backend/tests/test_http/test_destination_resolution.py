"""DNS結果の取得・全件検証・解決失敗の通知を単体で検証する。"""

import asyncio
import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.http.destination_policy import (
    DestinationRejectedError,
    DestinationRejectedReason,
    PublicIpAddress,
)
from app.http.destination_resolution import (
    _resolve_host,
    resolve_public_host_addresses,
)
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


class TestDnsResolution:
    @pytest.mark.asyncio
    async def test_extracts_addresses_from_requested_host(self, monkeypatch) -> None:
        """指定ホストのOS名前解決結果からIPv4・IPv6を順に取り出す。"""
        getaddrinfo = AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("2001:4860:4860::8888", 0, 0, 0),
                ),
            ]
        )
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)

        addresses = await _resolve_host("example.com")

        getaddrinfo.assert_awaited_once_with("example.com", None)
        assert addresses == ["8.8.8.8", "2001:4860:4860::8888"]


class TestResolvedHostAddresses:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("address", ["8.8.8.8", "2001:4860:4860::8888"])
    async def test_returns_validated_address(self, address: str) -> None:
        """公開IPへ解決されたホストは検証済みのIP型で返す。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=[address]),
        ):
            addresses = await resolve_public_host_addresses("example.com")

        assert addresses == (PublicIpAddress(address),)

    @pytest.mark.asyncio
    async def test_returns_all_public_addresses_in_resolution_order(self) -> None:
        """公開IPの複数結果はIPv4・IPv6の順序を変えず全件返す。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=["1.1.1.1", "2001:4860:4860::8888", "8.8.8.8"]),
        ):
            addresses = await resolve_public_host_addresses("example.com")

        assert addresses == (
            PublicIpAddress("1.1.1.1"),
            PublicIpAddress("2001:4860:4860::8888"),
            PublicIpAddress("8.8.8.8"),
        )

    @pytest.mark.asyncio
    async def test_host_rejection_keeps_destination_in_attributes(self) -> None:
        """非公開IPへ解決されたホストの拒否は、ホストと解決先を文面に出さず属性に残す。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=["10.0.0.1"]),
        ):
            with pytest.raises(DestinationRejectedError) as caught:
                await resolve_public_host_addresses("internal.example")

        error = caught.value
        assert (
            error.reason,
            error.host,
            error.address,
            str(error),
        ) == (
            DestinationRejectedReason.RESOLVED_TO_NON_PUBLIC,
            "internal.example",
            "10.0.0.1",
            "宛先が外部通信の方針で拒否されました",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "addresses",
        [
            pytest.param(["8.8.8.8", "10.0.0.1"], id="prohibited-ipv4-last"),
            pytest.param(["10.0.0.1", "8.8.8.8"], id="prohibited-ipv4-first"),
            pytest.param(["8.8.8.8", "::1"], id="prohibited-ipv6-last"),
            pytest.param(["::1", "8.8.8.8"], id="prohibited-ipv6-first"),
        ],
    )
    async def test_rejects_entire_result_if_any_address_is_prohibited(
        self, addresses: list[str]
    ) -> None:
        """公開IPを含んでいても禁止IPの位置・種類によらず全体を拒否する。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=addresses),
        ):
            with pytest.raises(DestinationRejectedError):
                await resolve_public_host_addresses("multihomed.example")


class TestHostResolutionFailure:
    @pytest.mark.asyncio
    async def test_dns_failure_preserves_original_cause(self, monkeypatch) -> None:
        """OSの名前解決失敗は通信準備段階のDNS失敗として伝え、元の例外を原因に残す。"""
        origin = socket.gaierror("Name or service not known")
        monkeypatch.setattr(
            asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=origin)
        )

        with pytest.raises(HttpTransportError) as caught:
            await resolve_public_host_addresses("nonexistent.invalid")

        assert (caught.value.failure, caught.value.__cause__) == (
            HttpTransportFailure(
                HttpTransportStage.PREPARATION,
                HttpTransportFailureReason.DNS_RESOLUTION,
            ),
            origin,
        )

    @pytest.mark.asyncio
    async def test_empty_result_is_unexpected_state(self) -> None:
        """空の解決結果は通信失敗に分類せず、ホスト名を出さない想定外の状態として伝える。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(RuntimeError) as caught:
                await resolve_public_host_addresses("empty.example")

        assert (type(caught.value), str(caught.value)) == (
            RuntimeError,
            "名前解決が成功したのに結果が空でした",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "addresses",
        [
            pytest.param(["not-an-ip"], id="invalid-only"),
            pytest.param(["8.8.8.8", "not-an-ip"], id="invalid-after-public"),
            pytest.param(["not-an-ip", "8.8.8.8"], id="invalid-before-public"),
        ],
    )
    async def test_invalid_result_is_unexpected_state(
        self, addresses: list[str]
    ) -> None:
        """IPではない結果を読み飛ばさず、値を出さない想定外の状態として伝える。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(return_value=addresses),
        ):
            with pytest.raises(RuntimeError) as caught:
                await resolve_public_host_addresses("invalid.example")

        assert (type(caught.value), str(caught.value)) == (
            RuntimeError,
            "名前解決の結果にIPとして読めない値がありました",
        )
